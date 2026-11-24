"""诊断日志仅记录阶段、关联标识与异常类型，避免凭据泄漏。"""
import logging

def failure(stage, error, record_id=''):
    logging.getLogger('pp6kit.delivery').warning('%s [%s]: %s', stage, record_id, type(error).__name__)
