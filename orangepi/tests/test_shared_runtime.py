import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import ast,json,pathlib,queue,tempfile,threading,time,unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from embodied_brain_collect.checkers.base import SessionReport
from embodied_brain_collect.checkers.emg import EmgChecker
from framework_qc import BoardImuChecker
from qc_policy import apply_policy

class SharedRulesTests(unittest.TestCase):
    def run_stream(self,kind,case):
        with tempfile.TemporaryDirectory() as td:
            path=pathlib.Path(td)/kind;path.mkdir()
            if kind=='board_imu':
                t=np.arange(1000)*.005
                if case=='duplicate':t[100]=t[99]
                if case=='gap':t[500:]+=.12
                np.savez(path/'board_imu.npz',timestamps=t,angular_velocity=np.zeros((1000,3)),linear_acceleration=np.zeros((1000,3)))
                report=BoardImuChecker().run(path)
            else:
                k=np.arange(15000)
                if case=='loss':k=k[k%5!=0]
                em=k%10!=9;et=k[em]*.00045;it=k[~em]*.00045
                if case=='duplicate':et[100]=et[99]
                if case=='gap':et[len(et)//2:]+=.12
                np.savez(path/'emg_left.npz',emg_timestamps=et,imu_timestamps=it,emg_sn=k[em]%256,imu_sn=k[~em]%256,emg_data=np.zeros((len(et),8)),imu_gyro=np.zeros((len(it),3)),imu_accel=np.zeros((len(it),3)))
                report=EmgChecker().run(path)
            raw=SessionReport(session_dir=td,streams={kind:report})
            effective,_=apply_policy(raw);self.assertEqual(effective.level,raw.level)
            return effective
    def test_IMU_duplicate_is_WARN(self):self.assertNotEqual(self.run_stream('board_imu','duplicate').level,'ERROR')
    def test_IMU_large_gap_is_ERROR(self):self.assertEqual(self.run_stream('board_imu','gap').level,'ERROR')
    def test_EMG_duplicate_is_ERROR(self):self.assertEqual(self.run_stream('emg_left','duplicate').level,'ERROR')
    def test_EMG_large_gap_is_ERROR(self):self.assertEqual(self.run_stream('emg_left','gap').level,'ERROR')
    def test_EMG_sequence_loss_is_original_WARN(self):self.assertNotEqual(self.run_stream('emg_left','loss').level,'ERROR')

class IMUAcquisitionTests(unittest.TestCase):
    def setUp(self):
        tree=ast.parse((project_paths.SRC/'acquisition/capture.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='BoardImu')
        namespace={'time':time,'threading':threading,'queue':queue}
        exec(compile(ast.Module(body=[cls],type_ignores=[]),'isolated_BoardImu','exec'),namespace)
        c=namespace['BoardImu'];self.obj=c.__new__(c)
        self.obj.owner=SimpleNamespace(gate=threading.RLock(),active=True,start_ns=0,start_wall_ns=0,fail=Mock(),warn=Mock())
        self.obj.seen=0;self.obj.ready=False;self.obj.finishing=threading.Event();self.obj.previous=10
        self.obj.jobs=queue.Queue(maxsize=1000);self.obj.accepted=0;self.obj.queue_peak=0
        self.obj.timestamp_duplicates=0;self.obj.timestamp_backsteps=0;self.obj.timestamp_anomaly_events=[]
    def feed(self,stamp):self.obj.callback(stamp,1,(1.,2.,3.),(4.,5.,6.))
    def test_duplicate_keeps_exact_sample_and_warns(self):
        self.feed(10);self.assertEqual(self.obj.jobs.get()[0],10);self.assertEqual(self.obj.timestamp_duplicates,1)
        self.obj.owner.fail.assert_not_called();self.obj.owner.warn.assert_called_once()
    def test_backstep_keeps_exact_sample_and_warns(self):
        self.feed(9);self.assertEqual(self.obj.jobs.get()[0],9);self.assertEqual(self.obj.timestamp_backsteps,1);self.obj.owner.fail.assert_not_called()
    def test_inactive_gate_still_rejects(self):
        self.obj.owner.active=False;self.feed(11);self.assertTrue(self.obj.jobs.empty())
    def test_writer_overflow_is_still_fatal(self):
        self.obj.jobs=queue.Queue(maxsize=1);self.feed(11);self.feed(12);self.obj.owner.fail.assert_called_once()

if __name__=='__main__':unittest.main(verbosity=2)
