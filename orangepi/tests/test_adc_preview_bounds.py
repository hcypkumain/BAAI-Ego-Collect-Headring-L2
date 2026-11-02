"""Raw ADC extrema are preview metadata only, independent of display filtering."""
import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import live_preview

class AdcBoundsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.pub=live_preview.Publisher(self.tmp.name);self.pub.close()
    def test_bounds_exclude_disabled_channel_and_invalid_samples(self):
        counts=[[8388607,1,2,3,4,5,6,7],[-8388608,-8388608,10,20,30,40,50,8388607],[0,999,999,999,999,999,999,999]]
        display=[[0]*8 for _ in counts]
        self.pub.eeg(display,[True,True,False],['unused']+list('abcdefg'),'general',100,adc_counts=counts)
        self.pub._publish_once();value=json.loads((Path(self.tmp.name)/'eeg.json').read_text())
        self.assertEqual(value['adc_count_bounds'][0],[-8388608,1]);self.assertEqual(value['adc_count_bounds'][-1],[7,8388607])
        self.assertEqual(len(value['adc_count_bounds']),7);self.assertEqual(value['adc_bounds_scope'],'latest_preview_batch')
        self.assertEqual(value['trace'][0][1],[0]*7);self.assertIsNone(value['trace'][2][1][0])
    def test_missing_counts_clear_prior_bounds_instead_of_reusing_old_rails(self):
        self.pub.eeg([[0]*8],[True],['unused']+list('abcdefg'),'raw',100,adc_counts=[[8388607]*8])
        self.pub.eeg([[0]*8],[True],['unused']+list('abcdefg'),'raw',101)
        _,signals=self.pub._snapshot();self.assertIsNone(signals['eeg']['adc_count_bounds'])
    def test_unrequested_preview_does_not_inspect_counts(self):
        class Invalid:
            def __iter__(self):raise AssertionError('No optional work without display demand')
        self.pub.demand.signals=frozenset()
        self.pub.eeg([[0]*8],[True],['unused']+list('abcdefg'),'raw',100,adc_counts=Invalid())
        self.assertNotIn('eeg',self.pub.signals)

if __name__=='__main__':unittest.main()
