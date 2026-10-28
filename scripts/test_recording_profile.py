"""Profile precedence and invalid-input checks; no camera access."""
import argparse
import copy
from pathlib import Path
import unittest

from recording_profile import KIT, legacy_profile, load_profile, resolve_record_args, validate_profile, storage_plan


class RecordingProfileTests(unittest.TestCase):
    def test_official_crf_profile_and_overrides(self):
        args = argparse.Namespace(config=KIT/'deprecated/config/mouse-4k45-official-crf.json', duration=60,
                                  output_dir=KIT.parent/'test-recordings', crf=22,
                                  segment_seconds=20, gop_frames=None)
        resolve_record_args(args)
        self.assertEqual((args.encoder, args.crf, args.gop_frames, args.b_frames),
                         ('libx265', 22, 250, 3))
        self.assertEqual(args.x265_params['rc-lookahead'], 10)
        self.assertEqual(args.x265_params['aq-strength'], 0)
        self.assertEqual(args.x265_preset, 'superfast')
        self.assertEqual(args.duration, 60)
        self.assertIsNone(args.storage_plan)
        self.assertNotIn('imaging', args.resolved_profile)
        for updates in ({'rate_control': 'capped_vbr'}, {'gop_frames': 20}, {'x265_params': {'crf': 21}},
                        {'x265_params': {'pools': '20:crf=0'}}, {'x265_preset': 'invalid'}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_profile(dict(args.resolved_profile, **updates))

    def test_hevc_trial_and_cli_override(self):
        args = argparse.Namespace(config=KIT/'deprecated/config/mouse-4k45-hevc-trial.json', duration=60,
                                  output_dir=KIT.parent/'test-recordings', nvenc_cq=26,
                                  segment_seconds=20, gop_frames=None)
        resolve_record_args(args)
        self.assertEqual((args.encoder, args.b_frames, args.gop_frames), ('hevc_nvenc', 3, 250))
        self.assertEqual((args.nvenc_preset, args.nvenc_cq), ('p5', 26))
        self.assertEqual((args.duration, args.segment_seconds), (60, 20))
        self.assertIsNone(args.storage_plan)
        self.assertIsNone(args.imaging)
        invalid = dict(args.resolved_profile, b_frames=4)
        with self.assertRaises(ValueError):
            validate_profile(invalid)
        invalid = dict(args.resolved_profile, nvenc_preset='invalid')
        with self.assertRaises(ValueError):
            validate_profile(invalid)

    def test_profile_has_recommended_values(self):
        profile, source = load_profile(KIT / 'deprecated/config/mouse-4k45-recommended.json')
        self.assertEqual((profile['fps'], profile['speed_level'], profile['gop_frames']), (45, 4, 45))
        self.assertEqual((profile['segment_seconds'], profile['duration_hours']), (3600, 72))
        self.assertEqual((profile['rate_control'], profile['bitrate_mbps'], profile['maxrate_mbps']), ('capped_vbr', 24, 26))
        self.assertEqual(len(source['sha256']), 64)

    def test_cli_override_preserves_other_profile_values(self):
        args = argparse.Namespace(config=KIT/'deprecated/config/mouse-4k45-recommended.json', duration=60,
                                  output_dir=KIT.parent/'test-recordings', gop_frames=90)
        resolve_record_args(args)
        self.assertEqual(args.duration, 60)
        self.assertEqual((args.fps, args.speed_level, args.gop_frames), (45, 4, 90))
        self.assertEqual(args.imaging['gamma'], 100)
        self.assertEqual(args.resolved_profile['duration_hours'], 1/60)
        self.assertTrue(args.profile_source['sha256'])

    def test_legacy_cli_remains_usable(self):
        args = argparse.Namespace(config=None, duration=10, output_dir=KIT.parent/'test-recordings',
                                  fps=45, speed_level=4, gop_frames=None)
        resolve_record_args(args)
        self.assertEqual((args.fps, args.gop_frames, args.encoder), (45, 90, 'libx264'))
        self.assertIsNone(args.imaging)

    def test_fps_override_selects_matching_camera_speed(self):
        args = argparse.Namespace(config=KIT/'deprecated/config/mouse-4k45-recommended.json', duration=60,
                                  output_dir=KIT.parent/'test-recordings', fps=30, gop_frames=None)
        resolve_record_args(args)
        self.assertEqual((args.fps, args.speed_level), (30, 2))
        profile = legacy_profile()
        profile['fps'] = 45
        with self.assertRaisesRegex(ValueError, 'speed_level'):
            validate_profile(profile)

    def test_bad_settings_fail_before_camera_access(self):
        baseline = legacy_profile()
        for key, value in [('fps', True), ('duration_hours', float('nan')), ('b_frames', 2),
                           ('encoder', 'misspelled'), ('output_dir', 'relative-path'),
                           ('output_dir', str(KIT/'recordings')), ('gop_frames', 0),
                           ('keep_awake', 'false'), ('unknown_parameter', 1),
                           ('rate_control', 'wrong'), ('maxrate_mbps', 20),
                           ('bitrate_mbps', float('nan')), ('buffer_mbits', 0),
                           ('max_recording_gb', -1)]:
            with self.subTest(key=key, value=value):
                profile = copy.deepcopy(baseline)
                profile[key] = value
                with self.assertRaises(ValueError):
                    validate_profile(profile)

    def test_storage_budget_under_decimal_tb_with_margin(self):
        profile, _ = load_profile(KIT/'deprecated/config/mouse-4k45-recommended.json')
        plan = storage_plan(profile)
        self.assertEqual(plan['target_video_bytes'], 777600000000)
        self.assertEqual(plan['ceiling_video_bytes_at_nominal_fps'], 842400000000)
        self.assertLess(plan['planned_recording_bytes_with_margin'], 950000000000)
        self.assertLess(plan['required_free_bytes'], 1000000000000)
        args = argparse.Namespace(config=KIT/'deprecated/config/mouse-4k45-recommended.json', duration=168*3600,
                                  output_dir=KIT.parent/'test-recordings', gop_frames=None)
        with self.assertRaisesRegex(ValueError, 'exceeds max_recording_gb'):
            resolve_record_args(args)


if __name__ == '__main__':
    unittest.main()
