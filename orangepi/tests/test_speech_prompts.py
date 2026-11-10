import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/audio'))
from speech_prompts import fixed_prompts, task_announcement


def constant_string(node):
    if isinstance(node,ast.Constant) and isinstance(node.value,str):return node.value
    if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add):
        left,right=constant_string(node.left),constant_string(node.right)
        if left is not None and right is not None:return left+right
    return None


class SpeechPromptTests(unittest.TestCase):
    def test_fixed_prompt_catalog_covers_literal_launcher_speech(self):
        launcher=Path(__file__).resolve().parents[1]/'src/orangepi_collect/control/launcher.py'
        tree=ast.parse(launcher.read_text())
        speech=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='say' and node.args:
                text=constant_string(node.args[0])
                if text:speech.append(text)
        fixed=set(fixed_prompts().values())
        self.assertTrue(set(speech)-fixed==set(),sorted(set(speech)-fixed))
        self.assertEqual(task_announcement('任务甲'),'本段任务参考图像，任务甲。目前尚未录制，请按图像准备场景。设备就绪后，点击开始采集。')

    def test_stage_prompts_have_stable_ids(self):
        prompts=fixed_prompts()
        for key in ('wrist_choose','wrist_manual','wrist_baseline','wrist_left','wrist_right','wrist_restore','wrist_done','wrist_retry',
                    'emg_manual','emg_done','emg_pair','emg_insert_unplug_all','emg_insert_wait_left','countdown_5','countdown_1',
                    'finish_regular','finish_emg_only','finish_deferred','saved_pending_qc'):
            self.assertIn(key,prompts)
        self.assertEqual(len(prompts),len(set(prompts)))


if __name__=='__main__':unittest.main()
