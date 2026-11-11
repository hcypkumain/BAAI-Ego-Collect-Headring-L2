"""Random draws preserve immutable reference proofs, retries and catalog writes."""
import io
import json
from email.message import Message
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

CONTROL = Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'
COMMON = CONTROL.parent/'common'
sys.path[:0] = [str(CONTROL), str(COMMON)]
from reference_catalog import ReferenceCatalog, CatalogError
from reference_controls import ReferenceControlsMixin
from reference_credentials import ReferenceCredentials
from reference_library_http import handle_reference_request
from phone_http import handle_phone_request
from PIL import Image


class Controller(ReferenceControlsMixin):
    pass


class Handler:
    def __init__(self, path, body=b'', authorized=True, key='', origin=None):
        self.path=path; self.rfile=io.BytesIO(body); self.connection=Mock()
        self.client_address=('10.0.0.9', 1234); self.headers=Message()
        self.headers['Host']='pi.test'; self.headers['Content-Length']=str(len(body))
        self.headers['Content-Type']='application/json'
        if key:self.headers['X-Reference-Key']=key
        if origin:self.headers['Origin']=origin
        self.authorized=lambda:authorized
        self.send=Mock()


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name); self.base=self.project/'orangepi'; self.base.mkdir()
        self.catalog=ReferenceCatalog(self.base, project=self.project)
        made=self.catalog.create_list('测试列表', self.catalog.revision); self.list_id=made['id']
        self.ids=[]
        for color in ('red', 'green', 'blue', 'yellow'):
            data=io.BytesIO(); Image.new('RGB',(16,12),color).save(data,'PNG')
            self.ids.append(self.catalog.upload(data.getvalue(),color+'.png',self.list_id,self.catalog.revision)['id'])
        self.controller=Controller(); c=self.controller
        c.refresh_reference_histories=Mock()
        c._reference_project=self.project; c._reference_base=self.base; c._reference_ledger=Mock()
        # This catalog-only test has no pending ledger reservations. Keep the
        # new reservation scan at its real empty-list boundary.
        c._reference_ledger.images.return_value=[]
        c.reference_catalog=self.catalog; c._reference_preferences={'selected_list_id':self.list_id}
        c._reference_preferences_error=None; c.reference_error=None
        c._reference_retry=None; c._reference_active=None; c._reference_snapshot=None
        c._reference_cli_image=None; c._reference_cli_force=False; c._reference_completed=()
        c._deferred_catalog_revision=None; c._reference_reap_outcome=None
        c.state='idle'; c.worker=None; c.quit=False; c.state_write=Mock(); c.voice=Mock()
        c._apply_reference_snapshot(self.snapshot())
        self.done=[]
        self.completed=patch('reference_controls._completed_paths',side_effect=lambda *args:tuple(self.done))
        self.completed.start(); self.addCleanup(self.completed.stop)

    def snapshot(self):
        return self.catalog.freeze_list(self.list_id,self.catalog.revision,include_completed=True)

    def draw(self,index):
        with patch('reference_controls.secrets.choice',side_effect=lambda entries:entries[index]) as choice:
            result=self.controller.reference_begin_preparation()
        return result,choice

    def finish(self,success):
        c=self.controller
        if success:self.done.append(c._reference_active['selection']['asset']['relative_path'])
        c._reference_reap_outcome='success' if success else 'retry'
        c.reference_reaped()

    def test_draw_uses_whole_pending_pool_and_keeps_original_proof(self):
        result,choice=self.draw(2)
        self.assertEqual(len(choice.call_args.args[0]),4)
        self.assertEqual(result['asset']['id'],self.ids[2])
        self.assertEqual(result['position'],3)
        self.assertEqual(result['queue_sha256'],self.snapshot()['queue_sha256'])
        self.assertEqual(len(result['asset']['sha256']),64)
        self.assertEqual(self.controller.reference_task_state()['selection_mode'],'random_without_replacement')

    def test_repeated_draw_during_active_segment_is_refused(self):
        self.draw(1)
        with self.assertRaises(ValueError):self.controller.reference_begin_preparation()
        self.assertEqual(self.controller._reference_active['selection']['asset']['id'],self.ids[1])

    def test_retry_keeps_chosen_image_and_all_other_candidates(self):
        result,_=self.draw(2); self.finish(False)
        self.assertEqual([entry['index'] for entry in self.controller._reference_entries],[2,0,1,3])
        with patch('reference_controls.secrets.choice',side_effect=AssertionError('Retry must not draw again')):
            retried=self.controller.reference_begin_preparation()
        self.assertEqual(result,retried)

    def test_success_only_removes_chosen_image_and_next_draw_is_new(self):
        result,_=self.draw(2); self.finish(True)
        self.assertEqual([e['index'] for e in self.controller._reference_entries],[0,1,3])
        second,choice=self.draw(2)
        self.assertEqual(second['asset']['id'],self.ids[3]); self.assertNotEqual(second['asset']['id'],result['asset']['id'])
        self.assertEqual(len(choice.call_args.args[0]),3)

    def test_completed_and_deleted_images_are_not_drawn(self):
        self.done.append(self.snapshot()['items'][1]['relative_path'])
        self.catalog.delete_image(self.ids[3],self.catalog.revision)
        self.controller.reference_refresh(force=True)
        result,choice=self.draw(1)
        self.assertEqual(result['asset']['id'],self.ids[2]); self.assertEqual(len(choice.call_args.args[0]),2)

    def test_active_image_delete_is_blocked_then_safe_delete_discards_retry(self):
        self.draw(2)
        with self.assertRaises(CatalogError) as caught:self.catalog.delete_image(self.ids[2],self.catalog.revision)
        self.assertEqual(caught.exception.code,'in_use')
        self.finish(False); self.catalog.delete_image(self.ids[2],self.catalog.revision)
        self.controller.reference_refresh()
        self.assertIsNone(self.controller._reference_retry)
        self.assertEqual(len(self.controller.images),3)

    def test_catalog_addition_does_not_change_frozen_retry(self):
        result,_=self.draw(2); self.finish(False)
        data=io.BytesIO(); Image.new('RGB',(16,12),'purple').save(data,'PNG')
        self.catalog.upload(data.getvalue(),'new.png',self.list_id,self.catalog.revision)
        self.controller.reference_refresh()
        self.assertEqual(len(self.controller._reference_entries),4)
        retried,_=self.draw(0); self.assertEqual(result,retried)
        self.finish(True)
        self.assertEqual(len(self.controller._reference_entries),4)

    def test_explicit_maintenance_image_still_uses_requested_first_image(self):
        c=self.controller;c._reference_cli_force=True;c._reference_cli_image=c.images[1]
        c._apply_reference_snapshot(self.snapshot())
        with patch('reference_controls.secrets.choice',side_effect=AssertionError('Explicit selection')):
            selected=c.reference_begin_preparation()
        self.assertEqual(selected['asset']['id'],self.ids[1])

    def test_empty_list_refuses_preparation(self):
        self.done.extend(item['relative_path'] for item in self.snapshot()['items'])
        self.controller.reference_refresh(force=True)
        with self.assertRaises(ValueError):self.controller.reference_begin_preparation()
    def test_manual_choice_can_explicitly_retake_completed_image_without_losing_pending_pool(self):
        from types import SimpleNamespace
        c=self.controller;self.done.append(self.snapshot()['items'][2]['relative_path']);c.reference_refresh(force=True)
        c.dashboard=SimpleNamespace(privacy=SimpleNamespace(snapshot=lambda:{'required':False}));c.selected_devices=('cam_ros_left',);c.prepare=Mock()
        c.select_reference_image({'image_id':self.ids[2],'catalog_revision':self.catalog.revision})
        self.assertEqual(c.image,self.project/self.snapshot()['items'][2]['relative_path']);c.prepare.assert_called_once()
        self.assertEqual(c.reference_task_state()['remaining'],3)

    def owner(self,state='idle',privacy=None):
        return SimpleNamespace(reference_catalog=self.catalog,reference_credentials=ReferenceCredentials(self.base),
            reference_completed_paths=lambda:(), display_state=lambda:{'state':state,'privacy':privacy or {'required':False,'stop_confirmed':True,'phase':'idle'}})

    def test_mobile_access_requires_control_authorization_and_same_origin(self):
        owner=self.owner()
        for authorized,origin,expected in [(False,None,401),(True,'http://other.test',403),(True,None,200)]:
            handler=Handler('/api/phone/reference-access',authorized=authorized,origin=origin)
            self.assertTrue(handle_phone_request(handler,owner,'GET'))
            self.assertEqual(handler.send.call_args.args[0],expected)
        self.assertEqual(handler.send.call_args.args[1]['access_code'],owner.reference_credentials.code())

    def test_mobile_access_pauses_during_capture_and_rejects_bad_query(self):
        for state,path,expected in [('recording','/api/phone/reference-access',423),('idle','/api/phone/reference-access?x=1',400)]:
            handler=Handler(path);handle_phone_request(handler,self.owner(state),'GET')
            self.assertEqual(handler.send.call_args.args[0],expected)

    def test_real_upload_thumbnail_delete_restore_and_stale_revision(self):
        owner=self.owner(); key=owner.reference_credentials.code()
        data=io.BytesIO();Image.new('RGB',(23,18),'cyan').save(data,'JPEG')
        old_revision=self.catalog.revision
        handler=Handler('/api/reference-upload?list_id='+self.list_id+'&revision='+str(old_revision)+'&name=phone.jpg',data.getvalue(),key=key)
        handler.headers.replace_header('Content-Type','application/octet-stream')
        handle_reference_request(handler,owner,'POST');self.assertEqual(handler.send.call_args.args[0],201)
        identifier=handler.send.call_args.args[1]['id']
        handler=Handler('/api/reference-thumbnail?id='+identifier,key=key)
        handle_reference_request(handler,owner,'GET');self.assertEqual(handler.send.call_args.args[0],200)
        self.assertTrue(handler.send.call_args.args[1].startswith(b'\xff\xd8'))
        for action,revision,expected in [('delete_image',old_revision,409),('delete_image',self.catalog.revision,200),('restore_image',self.catalog.revision+1,200)]:
            body=json.dumps({'action':action,'id':identifier,'revision':revision}).encode()
            handler=Handler('/api/reference-library',body,key=key)
            handle_reference_request(handler,owner,'POST');self.assertEqual(handler.send.call_args.args[0],expected)
        self.assertTrue(self.catalog.asset_path(identifier).is_file())


if __name__=='__main__':unittest.main()
