#!/usr/bin/env python3
"""Exercise a disposable monitor server with a small model; never target a user's active server."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,required=True)
    parser.add_argument('--backend',choices=['llamacpp','ninfer'],default='llamacpp')
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--ui-dir',type=Path,required=True)
    parser.add_argument('--gpu',required=True,help='device UUID')
    parser.add_argument('--port',type=int,default=18409)
    parser.add_argument('--log',type=Path,required=True)
    args=parser.parse_args()
    base=f'http://127.0.0.1:{args.port}'
    def call(path,body=None,authorized=True):
        headers={'Authorization':'Bearer monitor-test-key'} if authorized else {}
        data=json.dumps(body).encode() if body is not None else None
        if data is not None:headers['Content-Type']='application/json'
        return urllib.request.urlopen(urllib.request.Request(base+path,data=data,headers=headers),timeout=20)
    def snapshot():
        with call('/api/monitor/snapshot') as response:
            assert response.headers['Cache-Control']=='no-store'
            return json.load(response)
    def wait_for(predicate,seconds=30):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            data=snapshot()
            if predicate(data):return data
            time.sleep(.1)
        raise AssertionError('monitor state did not converge')
    command=[str(args.binary.resolve()),'-m',str(args.model.resolve()),'--host','127.0.0.1','--port',str(args.port),
        '-c','4096','-n','512','-b','512','-ngl','99','--kvmem','--kvmem-budget','2048',
        '--kvmem-gen-reserve','512','--reasoning-budget','0','--api-key','monitor-test-key','--ui-dir',str(args.ui_dir.resolve())]
    if args.backend=='ninfer':
        command=[str(args.binary.resolve()),str(args.model.resolve()),'--host','127.0.0.1','--port',str(args.port),
            '--max-context','4096','--default-max-tokens','512','--prefill-chunk','128','--kvmem-budget','512','--kvmem-gen-reserve','128',
            '--kvmem-host-mib','128','--kv-dtype','int8',
            '--no-thinking','--api-key','monitor-test-key','--log-stats-panel','off']
    env=dict(os.environ,CUDA_VISIBLE_DEVICES=args.gpu,CUDA_DEVICE_ORDER='PCI_BUS_ID')
    with args.log.open('w',encoding='utf-8') as log:
        process=subprocess.Popen(command,env=env,stdout=log,stderr=log)
        try:
            for _ in range(600):
                if process.poll() is not None:raise RuntimeError(f'server exited: {process.returncode}; see {args.log}')
                try:
                    with call('/health',authorized=False):break
                except (OSError,urllib.error.URLError):time.sleep(.1)
            else:raise TimeoutError('server startup')
            for path in ('/monitor/','/monitor','/monitor/monitor.js','/monitor/monitor.css'):
                with call(path,authorized=False) as response:assert response.status==200
            try:call('/api/monitor/snapshot',authorized=False)
            except urllib.error.HTTPError as error:assert error.code==401
            else:raise AssertionError('snapshot was public')
            def generate():
                with call('/v1/chat/completions',{'messages':[{'role':'user','content':'Write a numbered list of 100 different animals.'}], 'max_tokens':256,'stream':False,'chat_template_kwargs':{'enable_thinking':False}}) as response:
                    return json.load(response)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result=pool.submit(generate)
                running=wait_for(lambda s: bool(s['active']))
                poll_started=time.monotonic();snapshot();assert time.monotonic()-poll_started<2
                response=result.result(timeout=120)
            final=wait_for(lambda s:bool(s['recent']) and not s['active'])
            row=final['recent'][0]
            assert row['status']=='completed',row
            assert row['generated_tokens']==response['usage']['completion_tokens']>0,(row,response['usage'])
            assert row['prompt_tokens']==response['usage']['prompt_tokens']
            assert row['decode_tps']>0
            try:call('/v1/chat/completions',{'messages':'invalid'})
            except urllib.error.HTTPError as error:assert error.code==400
            else:raise AssertionError('invalid request accepted')
            if args.backend=='ninfer':
                # NInfer emits lifecycle records after protocol parsing. Exercise a real
                # preparation rejection (context overflow), not a pre-lifecycle JSON error.
                try:call('/v1/chat/completions',{'messages':[{'role':'user','content':' word'*5000}], 'max_tokens':32})
                except urllib.error.HTTPError as error:assert error.code==400
                else:raise AssertionError('oversized prompt accepted')
            wait_for(lambda s:s['recent'][0]['status']=='error')
            stream=call('/v1/chat/completions',{'messages':[{'role':'user','content':'Write a long story about the ocean.'}], 'max_tokens':512,'stream':True,'chat_template_kwargs':{'enable_thinking':False}})
            for line in stream:
                if b'"content":"' in line:break
            stream.close()
            disconnected=wait_for(lambda s:not s['active'])['recent'][0]
            assert disconnected['status']=='disconnected',disconnected
            hardware=wait_for(lambda s:s['hardware']['cpu_percent'] is not None)['hardware']
            assert any(g['uuid']==args.gpu for g in hardware['gpus']),hardware
            with call('/props') as prop_response:
                devices=json.load(prop_response)['execution']['monitor_devices']
            assert len(devices)==1,devices
            selected_gpu=next(g for g in hardware['gpus'] if g['uuid']==args.gpu)
            if args.backend=='ninfer':
                assert devices[0]['uuid']==args.gpu,devices
            else:
                def pci(value):
                    return tuple(int(part,16) for part in value.replace('.',':').split(':'))
                assert pci(devices[0]['pci_bus_id'])==pci(selected_gpu['pci_bus_id']),devices
            print(json.dumps({'result':'PASS','backend':args.backend,'gpu':args.gpu,'tokens':row['generated_tokens'],
                'checks':['public assets','API auth','nonblocking poll during generation','usage agreement','validation error','stream disconnect','hardware','inference device identity'],
                'hardware':hardware},ensure_ascii=False,indent=2))
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()

if __name__=='__main__':main()
