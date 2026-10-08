"""Bundled, source-verified TXT review materials and reusable submission text."""
import os
import tempfile
import zipfile
from pathlib import Path
from .app_paths import app_root

RULE_FILE = '漫画AI审核总规则_V1.md'
PROJECT_FILE = '漫画AI审核_项目指令.txt'
HELP_FILE = '漫画AI审核_使用说明.txt'
MATERIALS_DIR = app_root() / 'resources' / 'ai_review'
SUBMISSION = ('请完整阅读《漫画AI审核总规则_V1.md》和配套项目指令，再审核本轮上传的全部INPUT TXT。\n'
    '任务编号、分片、逐本CONTROL及返回模板均从当前INPUT读取。补充依据使用INPUT中的任务快照，'
    '仅将每本已匹配的SUPPLEMENTAL用于该本，不套用其他任务或后续编辑的条件。\n'
    '一个INPUT对应一个UTF-8 RESULT TXT，文件名末尾_INPUT.txt改为_RESULT.txt。'
    '逐本返回完整合法JSON及所有结束标记，多个INPUT分别生成，不合并。'
    '完成后直接提供各RESULT文件下载链接；无法读完整资料或生成文件时如实说明，不虚构链接。')


def material_text(filename):
    return (MATERIALS_DIR/filename).read_text(encoding='utf-8-sig')


def export_materials(destination):
    destination = Path(destination)
    contents = {name:(MATERIALS_DIR/name).read_bytes() for name in (RULE_FILE,PROJECT_FILE,HELP_FILE)}
    contents['漫画AI审核_送审提示词.txt'] = SUBMISSION.encode('utf-8')
    fd,name = tempfile.mkstemp(dir=destination.parent,prefix='.review_materials_',suffix='.zip')
    try:
        os.close(fd)
        with zipfile.ZipFile(name,'w',zipfile.ZIP_DEFLATED) as archive:
            for filename,body in contents.items(): archive.writestr(filename,body)
        os.replace(name,destination)
    finally: Path(name).unlink(missing_ok=True)
