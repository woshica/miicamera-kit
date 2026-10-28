"""Offline browser workbench over the existing capture process; stdlib only."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid
import webbrowser

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / 'scripts'))
from operator_config import OperatorConfig


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class Workbench:
    """Serializes settings and camera ownership; children own acquisition/encoding."""
    def __init__(self, settings, kit=KIT, work_dir=None, popen=subprocess.Popen):
        self.settings, self.kit, self.popen = settings, Path(kit), popen
        identity = hashlib.sha256(str(self.kit.resolve()).encode()).hexdigest()[:12]
        self.work_dir = Path(work_dir or Path(tempfile.gettempdir()) / ('miicamera-workbench-' + identity))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.process = self.control = self.log = self.profile = None
        self.official_process = None
        self.mode = 'idle'
        self.transition = False
        self.closing = False
        self.selected_name = None
        self._rate_sample = None
        self._recent_mbps = None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def _status(self):
        if self.control is None:
            return {'stage': 'idle'}
        try:
            state = json.loads((self.control / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {'stage': 'starting'}
        if self.process is not None and self.process.poll() is not None:
            active = self.work_dir/'active-child.json'
            if active.exists():
                active.unlink()
            if self.log is not None:
                self.log.close()
                self.log = None
            # A child crash or an early SDK failure must never look like success.
            if self.process.returncode or state.get('stage') not in ('complete', 'incomplete'):
                state['stage'] = 'incomplete'
                state['error'] = state.get('error') or f'程序退出，退出码 {self.process.returncode}；请查看运行日志。'
        return state

    def state(self):
        with self.lock:
            state = self._status()
            config = self.settings.view()
            busy = self.running()
            output = Path(self.profile['output_dir'] if self.profile else config['daily']['output_dir'])
            disk_path = output
            while not disk_path.exists() and disk_path != disk_path.parent:
                disk_path = disk_path.parent
            try:
                free_gb = shutil.disk_usage(disk_path).free / 1e9
            except OSError:
                free_gb = None
            run = Path(state['run_dir']) if state.get('run_dir') else None
            files = sorted(run.glob('segment-*.mkv')) if run and run.is_dir() else []
            try:
                size = sum(p.stat().st_size for p in files)
            except OSError:
                size = 0
            now = time.monotonic()
            if self._rate_sample and now - self._rate_sample[0] >= 1:
                self._recent_mbps = max(0, size-self._rate_sample[1])*8/(now-self._rate_sample[0])/1e6
                self._rate_sample = (now, size)
            elif self._rate_sample is None:
                self._rate_sample = (now, size)
            elapsed = state.get('elapsed_s', 0) or 0
            planned = self.profile['duration_hours']*3600 if self.profile else 0
            state.update(mode=self.mode, running=busy, transition=self.transition,
                         official_running=self.official_running(),
                         locked=self.transition or (busy and self.mode == 'record'),
                         config=config, selected_name=self.selected_name,
                         remaining_s=max(0, planned-elapsed), planned_s=planned,
                         free_gb=free_gb, video_gb=size/1e9, segments=len(files),
                         average_mbps=size*8/elapsed/1e6 if elapsed else None,
                         recent_mbps=self._recent_mbps,
                         session_id=self.control.name if self.control else None,
                         preview_available=bool(self.control and (self.control/'preview.png').exists()),
                         log_path=str(self.control/'process.log') if self.control else None)
            return state

    def _ensure_editable(self):
        if self.closing:
            raise ValueError('工作台正在关闭，请等待视频保存结束。')
        if self.transition or (self.running() and self.mode == 'record'):
            raise ValueError('本轮录制已锁定方案。请先停止并等待文件保存及检查完成。')

    def official_running(self):
        return self.official_process is not None and self.official_process.poll() is None

    def save_imaging(self, data):
        from camera_profile import validate_imaging
        # Reject malformed values before disturbing a running preview.
        imaging = validate_imaging(data['imaging'])
        with self.lock:
            self._ensure_editable()
            old_process = self.process if self.running() else None
            self.transition = True
            if old_process:
                (self.control/'stop.request').touch()
        try:
            if old_process:
                old_process.wait(timeout=15)
            with self.lock:
                result = self.settings.save_imaging(imaging, data.get('daily'), data.get('scheme_values'))
                if old_process:
                    self._launch('preview')
                return result
        except subprocess.TimeoutExpired as exc:
            raise ValueError('相机尚未释放，本次未保存画面配置。请稍后重试。') from exc
        finally:
            with self.lock:
                self.transition = False

    def open_official(self):
        executable = self.kit/'runtime/imageview/x64/imageview.exe'
        with self.lock:
            self._ensure_editable()
            if self.official_running():
                return
            if not executable.is_file():
                raise ValueError('找不到官方 ImageView，请检查完整软件包中的 runtime/imageview。')
            old_process = self.process if self.running() else None
            self.transition = True
            if old_process:
                (self.control/'stop.request').touch()
        try:
            if old_process:
                old_process.wait(timeout=15)
            with self.lock:
                # ImageView must keep its x64 working directory for language/resources.
                self.official_process = self.popen([str(executable)], cwd=executable.parent,
                                                   stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('相机预览尚未退出，未打开官方软件。请稍后重试。') from exc
        finally:
            with self.lock:
                self.transition = False

    def save(self, data):
        with self.lock:
            self._ensure_editable()
            old = self.settings.view()['daily']['scheme']
            if self.running() and data['daily']['scheme'] != old:
                raise ValueError('切换方案前请先停止预览，再打开新方案的预览。')
            result = self.settings.save(data['daily'], data.get('scheme_values'))
            if self.running():
                profile = self.settings.resolved()
                write_json(self.control/'settings.json', {'exposure_us': profile['exposure_us'], 'gain': profile['gain']})
            return result

    def create_scheme(self, data):
        with self.lock:
            self._ensure_editable()
            if self.running():
                raise ValueError('请先停止预览，再另存新方案。当前方案已经保存，可直接复制。')
            return self.settings.create_scheme(data['name'], data['source_id'],
                                               data.get('scheme_values'), data.get('daily'))

    def _launch(self, mode, short=False):
        profile = self.settings.resolved(short=short)
        # Browser preview shares captured frames; avoid launching a second player.
        profile['preview_enabled'] = False
        profile['status_enabled'] = True
        directory = self.work_dir / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        directory.mkdir()
        config = self.settings.view()
        self.selected_name = config['schemes'][config['daily']['scheme']]['name']
        profile['profile'] = self.selected_name
        profile['description'] = f"工作台方案 {config['daily']['scheme']}；开始时固定，整轮分段不切换。"
        write_json(directory/'profile.json', profile)
        write_json(directory/'owner.json', {'pid': os.getpid()})
        write_json(directory/'operator-selection.json', {'scheme_id': config['daily']['scheme'],
                   'scheme_name': self.selected_name, 'short_test': short})
        command = [str(self.kit/'runtime/python/python.exe'), '-B', '-X', 'utf8', '-u']
        if mode == 'record':
            command += [str(self.kit/'scripts/capture.py'), 'record']
        else:
            command += [str(self.kit/'scripts/operator_camera.py')]
        command += ['--config', str(directory/'profile.json'), '--control-dir', str(directory)]
        log = (directory/'process.log').open('wb')
        try:
            process = self.popen(command, cwd=self.kit, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception:
            log.close()
            raise
        if self.log:
            self.log.close()
        self.process, self.control, self.log = process, directory, log
        self.profile, self.mode = profile, mode
        if getattr(process, 'pid', None):
            write_json(self.work_dir/'active-child.json', {'pid': process.pid, 'control': str(directory)})
        self._rate_sample = self._recent_mbps = None

    def start(self, mode, short=False):
        # Release the lock while waiting for calibration; status remains readable.
        with self.lock:
            self._ensure_editable()
            if self.official_running():
                raise ValueError('请先关闭官方 ImageView，再使用工作台预览或录制。')
            if self.running() and self.mode == 'preview':
                if mode == 'preview':
                    return
                self.transition = True
                (self.control/'stop.request').touch()
                old_process = self.process
            else:
                self._launch(mode, short)
                return
        try:
            old_process.wait(timeout=15)
            with self.lock:
                if self._status().get('stage') != 'complete':
                    raise ValueError('预览未能正常结束，请先检查错误；本次未启动录像。')
                self._launch(mode, short)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('相机尚未释放，请等待预览结束后重试；未强行终止程序。') from exc
        finally:
            with self.lock:
                self.transition = False

    def stop(self):
        with self.lock:
            if self.transition:
                raise ValueError('正在从预览切换到录制，请等待状态更新后再停止。')
            if self.running():
                (self.control/'stop.request').touch()

    def shutdown(self):
        # A start request may be waiting for calibration outside the lock.
        # Keep this instance alive until that handoff finishes, then stop its owner.
        with self.lock:
            self.closing = True
        while True:
            with self.lock:
                if not self.transition:
                    if self.running():
                        (self.control/'stop.request').touch()
                    process = self.process
                    break
            time.sleep(.1)
        if process is not None:
            process.wait()
        self._status()

    def preview(self):
        with self.lock:
            if self.control is None:
                raise FileNotFoundError('尚未打开预览')
            return (self.control/'preview.png').read_bytes()

    def log_text(self):
        with self.lock:
            if not self.control:
                return ''
            with (self.control/'process.log').open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell()-16000))
                return stream.read().decode('utf-8', errors='replace')

    def open_output(self):
        with self.lock:
            state = self._status()
            path = Path(state.get('run_dir') or self.settings.resolved()['output_dir'])
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        super().__init__(address, Handler)
        self.app = app
        self.token = secrets.token_urlsafe(32)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, kind='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        return (self.headers.get('Host') == self.server.origin.removeprefix('http://')
                and self.headers.get('Origin', self.server.origin) == self.server.origin
                and secrets.compare_digest(self.headers.get('X-Camera-Token', ''), self.server.token))

    def do_GET(self):
        path = urlsplit(self.path).path
        if self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            return self.send(403, b'{}')
        static = {'/': ('scripts/operator_ui/index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('scripts/operator_ui/app.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('scripts/operator_ui/style.css', 'text/css; charset=utf-8'),
                  '/guide': ('docs/operator-guide.html', 'text/html; charset=utf-8')}
        try:
            if path in static:
                filename, kind = static[path]
                return self.send(200, (self.server.app.kit/filename).read_bytes(), kind)
            if not self.authorized():
                return self.send(403, b'{}')
            if path == '/api/state':
                return self.send(200, json.dumps(self.server.app.state(), ensure_ascii=False).encode())
            if path == '/api/preview':
                return self.send(200, self.server.app.preview(), 'image/png')
            if path == '/api/log':
                return self.send(200, json.dumps({'text': self.server.app.log_text()}, ensure_ascii=False).encode())
            return self.send(404, b'{}')
        except FileNotFoundError:
            self.send(404, b'{}')
        except Exception as exc:
            self.send(500, json.dumps({'error': str(exc)}, ensure_ascii=False).encode())

    def do_POST(self):
        if not self.authorized():
            return self.send(403, b'{}')
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384 or self.headers.get_content_type() != 'application/json':
                raise ValueError('无效请求')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('请求必须为对象')
            app = self.server.app
            path = urlsplit(self.path).path
            if path == '/api/save':
                app.save(data)
            elif path == '/api/create-scheme':
                app.create_scheme(data)
            elif path == '/api/save-imaging':
                app.save_imaging(data)
            elif path == '/api/open-official':
                app.open_official()
            elif path == '/api/preview':
                app.start('preview')
            elif path == '/api/record':
                if type(data.get('short', False)) is not bool:
                    raise ValueError('short 必须为布尔值')
                app.start('record', data.get('short', False))
            elif path == '/api/stop':
                app.stop()
            elif path == '/api/open-output':
                app.open_output()
            else:
                return self.send(404, b'{}')
            self.send(200, json.dumps({'ok': True}, ensure_ascii=False).encode())
        except Exception as exc:
            self.send(400, json.dumps({'error': str(exc)}, ensure_ascii=False).encode())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    settings = OperatorConfig(KIT)
    app = Workbench(settings)
    import msvcrt
    lock = (app.work_dir/'instance.lock').open('a+b')
    if (app.work_dir/'instance.lock').stat().st_size == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            # First launch may still be initializing before writing its URL.
            url = None
            for _ in range(20):
                try:
                    url = (app.work_dir/'instance.url').read_text(encoding='utf-8')
                    break
                except FileNotFoundError:
                    time.sleep(.1)
            if not args.no_browser:
                if url:
                    webbrowser.open(url)
            print('工作台已在运行，已尝试重新打开。请保留原来的运行窗口。')
            return 0
        finally:
            lock.close()
    active = app.work_dir/'active-child.json'
    if active.exists():
        # A forcibly closed console leaves the child time to save its queue.
        from operator_camera import parent_is_alive
        old = json.loads(active.read_text(encoding='utf-8'))
        if parent_is_alive(old['pid']):
            raise RuntimeError('上一次相机进程仍在保存或检查，请稍后再次启动。日志位置：' + old['control'])
    settings.initialize()
    server = Server(('127.0.0.1', 0), app)
    url = server.origin + '/#' + server.token
    (app.work_dir/'instance.url').write_text(url, encoding='utf-8')
    print('相机录制工作台已启动。请保留这个窗口；在网页中停止并保存录像。', flush=True)
    print('如果网页被关闭，可再次双击启动录制.cmd。无需联网。', flush=True)
    print(url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        print('正在保存和检查本轮视频，请等待，勿关闭窗口。', flush=True)
        app.shutdown()
    finally:
        server.server_close()
        lock.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'工作台未能启动：{exc}', file=sys.stderr)
        raise SystemExit(1)
