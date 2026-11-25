"""Real Board + real SDK against one local fake model; no external service."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import assist_board_server as board
import board_auth as auth
from llm_service import load_llm_config


@contextmanager
def serving(handler):
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
    worker.start()
    try:yield server.server_address[1]
    finally:
        server.shutdown();server.server_close();worker.join(2)
        assert not worker.is_alive()


def test_model_settings_roundtrip_updates_existing_sdk_then_revokes(tmp_path,monkeypatch):
    monkeypatch.setenv('NO_PROXY','*')
    monkeypatch.setenv('no_proxy','*')
    model_requests=[]
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*_args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            model_requests.append({'authorization':self.headers['Authorization'],'body':body})
            payload=json.dumps({'id':'synthetic-completion','object':'chat.completion',
                'created':1700000000,'model':body['model'],
                'choices':[{'index':0,'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    with serving(Model) as model_port:
        url=f'http://127.0.0.1:{model_port}/v1'
        old_key='synthetic-old-key-for-local-sdk'
        new_key='synthetic-new-key-for-local-sdk'
        config=tmp_path/'config.json'
        original={'collector':{'enabled':True,'untouched':'synthetic'},'llm':{
            'enabled':True,'active_provider':'custom','daily_token_budget':0,
            'providers':{'custom':{'api_key':old_key,'base_url':url,'model':'model-before',
                                   'protocol':'openai_compatible'}}}}
        config.write_text(json.dumps(original),encoding='utf-8')
        data=tmp_path/'data';data.mkdir()
        copy=data/'watch_synthetic.config.json'
        copy.write_text(json.dumps(dict(original,room={'label':'synthetic'})),encoding='utf-8')
        gate=data/'llm_runtime.json'
        gate.write_text('{"enabled":true,"purposes":{"realtime":true}}',encoding='utf-8')
        gate_before=gate.read_bytes()
        router=load_llm_config(str(copy)).build_router(budget_dir=str(data))
        held=router.active_client
        assert held.chat('synthetic','first')=='OK'
        assert model_requests[-1]['authorization']=='Bearer '+old_key
        secret='synthetic-local-auth-secret'
        class H(board.Handler):
            auth_db={'secret':secret,'users':[{'name':'tester'}]}
            config_path=str(config)
            store=board.Store(str(data),'2026-11-14')
        cookie=auth.cookie_header(auth.make_token('tester',secret)).split(';')[0]
        with serving(H) as port:
            def request(method,path,body=None):
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=12)
                conn.request(method,path,None if body is None else json.dumps(body),
                             {'Cookie':cookie,'Content-Type':'application/json'})
                response=conn.getresponse();raw=response.read();conn.close()
                assert old_key.encode() not in raw and new_key.encode() not in raw
                return response.status,json.loads(raw)
            status,view=request('GET','/api/llm')
            assert status==200 and view['providers'][0]['has_key']
            draft={'provider':'custom','protocol':'openai_compatible','base_url':url,
                   'model':'model-after','api_key':new_key}
            assert request('POST','/api/llm/settings',draft)[1]['ok']
            assert len(model_requests)==1  # Saving alone must not test/enable/call a model.
            assert held.chat('synthetic','after replacement')=='OK'
            assert model_requests[-1]['authorization']=='Bearer '+new_key
            assert model_requests[-1]['body']['model']=='model-after'
            assert 'thinking' not in model_requests[-1]['body']
            assert json.loads(copy.read_text())['room']=={'label':'synthetic'}
            assert json.loads(config.read_text())['collector']==original['collector']
            draft.pop('api_key')
            assert request('POST','/api/llm/settings',draft)[1]['ok']
            assert json.loads(config.read_text())['llm']['providers']['custom']['api_key']==new_key
            before=len(model_requests)
            status,result=request('POST','/api/llm/test',draft)
            assert status==200 and result['ok'] and result['transport']=='direct'
            assert len(model_requests)==before+1
            assert model_requests[-1]['authorization']=='Bearer '+new_key
            assert model_requests[-1]['body']['max_tokens']<=8
            assert 'thinking' not in model_requests[-1]['body']
            assert request('POST','/api/llm/key/delete',{'provider':'custom'})[1]['ok']
            assert held.chat('synthetic','after deletion') is None
            assert router.chat('synthetic','after deletion') is None
            assert len(model_requests)==before+1
            status,view=request('GET','/api/llm')
            assert status==200 and view['providers'][0]['has_key'] is False
            assert request('GET','/api/health')[1]['ok']
            assert request('GET','/api/live')[0]==200
            assert gate.read_bytes()==gate_before
