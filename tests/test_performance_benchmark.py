"""Check packet continuity, rejection of corrupt data and transparent tail metrics."""
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('benchmark',Path(__file__).parents[1]/'tools/performance_benchmark.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
class BenchmarkTests(unittest.TestCase):
 def test_hud_packet_pairs_and_log_envelope(self):
  rows,issues,packets=b.parse_hud(['{"eventMessage":"metal-HUD: 10,12,34,10,2,20,3","timestamp":"t"}','metal-HUD: 12,12,34,100,4'])
  self.assertFalse(issues);self.assertEqual(packets,2);self.assertEqual([r['frame']for r in rows],[10,11,12]);self.assertEqual(rows[0]['packet_timestamp'],'t')
 def test_tail_metrics_are_not_average_instantaneous_fps(self):
  rows=[dict(interval_ms=x,gpu_ms=1)for x in [10,10,10,100]];s=b.summarize(rows)
  self.assertAlmostEqual(s['average_fps'],4/.13);self.assertEqual(s['low_1_percent_fps'],10);self.assertEqual(s['stutters']['50']['count'],1);self.assertEqual(s['stutters']['100']['count'],0)
 def test_log_command_banner_is_not_a_packet(self):
  rows,issues,packets=b.parse_hud(['Filtering the log data using "composedMessage CONTAINS metal-HUD:"','metal-HUD: 1,1,2,10,1'])
  self.assertFalse(issues);self.assertEqual(packets,1)
 def test_corrupt_packet_is_rejected_as_a_whole(self):
  for text in ['metal-HUD: 1,1,2,10','metal-HUD: 1,1,2,nan,1','metal-HUD: 1,1,2,-1,1']:
   rows,issues,_=b.parse_hud([text]);self.assertFalse(rows);self.assertTrue(issues)
 def test_overlap_and_missing_frames_are_not_silently_hidden(self):
  _,issues,_=b.parse_hud(['metal-HUD: 1,1,2,10,1','metal-HUD: 1,1,2,10,1','metal-HUD: 5,1,2,10,1'])
  self.assertEqual(len(issues),2)
if __name__=='__main__':unittest.main()
