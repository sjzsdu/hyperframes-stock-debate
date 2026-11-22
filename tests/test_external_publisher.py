"""AC-05: six-platform external/runtime compatibility and cross-entrypoint account lock."""
from pathlib import Path
import subprocess

import pytest

from stocktalk.modules.publisher import PLATFORM_LABELS, SauPublisher, PublishError
from stocktalk.modules.runtime_lock import account_lock, AccountBusy


@pytest.mark.parametrize('platform',list(PLATFORM_LABELS))
def test_external_runtime_preserves_argv_cwd_schedule_ai_covers_and_account(tmp_path,platform):
    old=tmp_path/'legacy';new=tmp_path/'external'
    for root in (old,new):
        (root/'.venv/bin').mkdir(parents=True)
        (root/'.venv/bin/sau').write_text('#!/bin/sh\nexit 0\n')
    video=tmp_path/'video.mp4';video.write_bytes(b'fixture')
    portrait=tmp_path/'portrait.png';portrait.write_bytes(b'fixture')
    landscape=tmp_path/'landscape.png';landscape.write_bytes(b'fixture')
    wide=tmp_path/'wide.png';wide.write_bytes(b'fixture')
    script={'title':'示例股票（000001）：观察业务与风险','turns':[]}
    def run(root,explicit):
        calls=[]
        config={'publish':{'sau_dir':str(root),'accounts':{platform:'fixture-account'},'preflight':True,
                           'retries':0,'ai_content_label':{platform:'AI生成'}}}
        if explicit:config['publish']['sau_executable']=str(root/'.venv/bin/sau')
        def runner(argv,cwd):
            calls.append((list(argv),cwd))
            return subprocess.CompletedProcess(argv,0,stdout='success',stderr='')
        publisher=SauPublisher(config,runner=runner)
        report=publisher.publish(video,script,'示例股票','000001',platforms=[platform],schedule='2027-01-01 18:00',
                                 platform_videos={platform:video},covers={'portrait':portrait,'landscape':landscape,'wide':wide})
        assert report['succeeded']==[platform]
        assert all(cwd==root for _,cwd in calls)
        return [argv[1:] for argv,_ in calls]
    legacy=run(old,False);external=run(new,True)
    assert legacy==external
    upload=next(a for a in external if 'upload-video' in a)
    assert upload[upload.index('--account')+1]=='fixture-account'
    assert upload[upload.index('--schedule')+1]=='2027-01-01 18:00'


def test_missing_external_executable_and_cross_process_account_lock(tmp_path,monkeypatch):
    monkeypatch.setattr('shutil.which',lambda _:None)
    publisher=SauPublisher({'publish':{'sau_dir':str(tmp_path),'sau_executable':str(tmp_path/'absent')}})
    with pytest.raises(PublishError):publisher._require_sau()
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    with account_lock('bilibili','fixture'):
        with pytest.raises(AccountBusy):
            with account_lock('bilibili','fixture'):pass
    with account_lock('bilibili','fixture'):pass
