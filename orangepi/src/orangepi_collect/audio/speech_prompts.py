"""Stable, non-personal prompts suitable for optional cloud prewarming."""
import importlib.util
from pathlib import Path
import sys

COMMON=Path(__file__).resolve().parents[1]/'common'
if str(COMMON) not in sys.path:sys.path.insert(0,str(COMMON))
import emg_calibration
import emg_insertion
import wrist_calibration
import project_paths


def task_announcement(title):
    title=''.join(ch for ch in str(title or '') if ch.isprintable()).strip()[:100] or '当前任务'
    return '本段任务参考图像，'+title+'。目前尚未录制，请按图像准备场景。设备就绪后，点击开始采集。'


def fixed_prompts():
    prompts={}
    for name,text in wrist_calibration.PROMPTS.items():prompts['wrist_'+name]=text
    for name,text in emg_calibration.PROMPTS.items():prompts['emg_'+name]=text
    for name,text in emg_insertion.SPEECH.items():prompts['emg_insert_'+name]=text
    prompts.update({
        'startup_confirm':'Orange Pi 采集程序。当前接入的数据采集设备：尚未发现采集设备。',
        'startup_missing':'未收到有效数据。请检查连接。',
        'startup_emg_no_data':'肌电接收器已识别，但未收到有效肌电。请检查臂环电源和连接，程序会自动重试。',
        'startup_required_devices_missing':'必需采集设备未全部在线。当前不能确认设备，也不能开始采集。请检查连接后重新扫描。',
        'startup_confirm_devices':'必需采集设备均已在线。请点击确认设备，或选择重新扫描。',
        'start_missing_screen':'请先打开触控采集界面，确认屏幕已连接。',
        'start_not_ready':'设备尚未就绪，请先准备本段。',
        'start_impedance':'请先结束阻抗检查、确认结果，并等待普通脑电采样恢复。',
        'start_impedance_not_restored':'请先完成脑电阻抗检查，并确认检测已结束、设备配置已恢复。',
        'start_camera_check':'请先完成全部相机同屏检查，并确认头环和腕部相机方向。',
        'start_screen_black':'触控屏未确认黑屏，本次没有开始采集。请检查屏幕连接后重试。',
        'start_impedance_changed':'脑电阻抗检查状态已变化，请检查后再开始。',
        'start_preparing':'设备尚未就绪，请等待准备完成后再点击开始采集。',
        'ready':'设备已经就绪，目前尚未录制。请充分查看参考图，再手动检查相机。确认画面后返回准备页，点击开始正式采集才会倒计时。',
        'reference_exhausted':'当前所有参考图像已采集完成。点击准备本段，重新检查待采集图片。',
        'data_gap':'检测到数据不连续，异常已记录，请查看检验报告。',
        'stop_saved':'采集已停止。请点击确认保存，或选择重采本段。',
        'stop_error':'采集已停止，但设备或文件收尾发生错误；数据未标记为成功，请查看日志。',
        'finish_regular':'采集已停止，正在保存并检验，请等待。',
        'finish_emg_only':'采集已停止，正在保存并完成肌电/脑电检查，其余待主机质检。',
        'finish_deferred':'采集已停止，正在保存并标记待主机质检。',
        'discard_wait':'正在停止设备并清理本段文件，请等待。清理后由您手动准备下一段。',
        'discard_done':'本段已放弃，文件已清理。请手动点击准备本段。',
        'failed_kept':'本条已标记失败并保留。请手动点击重新准备本段。',
        'not_used':'本条未标记已用。请点击重采本段，或重复播报错误排查提示。',
        'saved_pending_qc':'本段已保存，等待主机质检。请手动点击准备下一段。',
        'saved_qc_complete':'本段已保存并完成检验。可以回看录像，或手动点击准备下一段。',
        'entry':'已进入采集程序。点击准备本段查看任务参考图，或点击重复提示。',
        'devices_confirmed':'已确认本次采集设备。点击准备本段，查看任务参考图像并准备录制。',
        'no_devices':'未检测到任何采集设备。请检查连接后点击重新扫描。',
        'countdown_5':'五','countdown_4':'四','countdown_3':'三','countdown_2':'二','countdown_1':'一',
        'troubleshooting_generic':'请查看当前会话日志中的错误详情，排查后点击重新准备。',
    })
    try:
        path=project_paths.PROJECT/'src/embodied_brain_collect/session/troubleshooting.py'
        spec=importlib.util.spec_from_file_location('speech_prompt_troubleshooting',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for name,text in module.SLOT_GUIDES.items():prompts['troubleshooting_'+name]=text
    except (OSError,ImportError,AttributeError):pass
    return prompts
