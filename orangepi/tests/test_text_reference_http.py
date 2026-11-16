import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
sys.path[:0]=[str(ROOT/'common'),str(ROOT/'control')]
import dashboard
from text_reference import canonical,validate

class TextReferenceHttpTests(unittest.TestCase):
 def test_text_reference_is_json_and_privacy_is_enforced(self):
  with tempfile.TemporaryDirectory() as temp:
   project=Path(temp);base=project/'orangepi';(base/'runtime').mkdir(parents=True);ref=project/'configs/environments/_uploads/_temporary_text/temporary-test/text_00000000000000000000000000000001/occurrence_0001.text.json';ref.parent.mkdir(parents=True)
   payload={'version':1,'namespace':'temporary-test','source_sha256':'a'*64,'task_id':'text_00000000000000000000000000000001','scene':'洗衣区','name':'清洗','reference_text':'第一步\n第二步','occurrence':1,'target_count':50};ref.write_bytes(canonical(validate(payload)))
   ui=dashboard.Dashboard(base,base,{'host':'127.0.0.1','port':0,'access_code_required':False});self.addCleanup(ui.close);url='http://127.0.0.1:'+str(ui.server.server_port)+'/api/reference';ui.publish({'state':'ready','revision':1,'image':str(ref),'reference_image_state':{'kind':'text'}})
   with patch.object(ui.privacy,'snapshot',return_value={'required':False,'stop_confirmed':True,'phase':'idle'}):
    with urlopen(url,timeout=3) as response:
     self.assertTrue(response.headers['Content-Type'].startswith('application/json'));data=json.load(response);self.assertEqual(data['kind'],'text');self.assertEqual(data['reference_text'],'第一步\n第二步')
   with patch.object(ui.privacy,'snapshot',return_value={'required':True,'stop_confirmed':False,'phase':'recording'}):
    with self.assertRaises(HTTPError) as error:urlopen(url,timeout=3)
    self.assertEqual(error.exception.code,423)
if __name__=='__main__':unittest.main()
