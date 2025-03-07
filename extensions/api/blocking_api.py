import json
import torch
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from extensions.api.util import build_parameters, try_start_cloudflared
from modules import shared
from modules.chat import generate_chat_reply, generate_chat_prompt
from modules.LoRA import add_lora_to_model
from modules.models import load_model, unload_model
from modules.models_settings import (
    get_model_settings_from_yamls,
    update_model_parameters
)
from modules.text_generation import (
    encode,
    generate_reply,
    stop_everything_event
)
from modules.utils import get_available_models

def calc_perplexity_v1(text):
    encodings = encode(text, add_special_tokens=False)
    seq_len = encodings.shape[1]
    if hasattr(shared.model.config, 'max_position_embeddings'):
        max_length = shared.model.config.max_position_embeddings
    else:
        max_length = 4096

    input_ids = encodings[:, :]
    target_ids = input_ids.clone()
    # target_ids[:, :-target_len] = -100

    with torch.no_grad():
        outputs = shared.model(input_ids=input_ids, labels=target_ids)
        logits = outputs.logits
    
    assert len(logits.shape) == 3   # 1, seq_len, tokens
    assert logits.shape[0] == 1
    assert len(encodings.shape) == 2
    assert encodings.shape[0] == 1
    logs = logits[0, :-1].to(device=encodings.device)
    logs -= torch.logsumexp(logs, dim=1, keepdim=True)
    index = encodings[0, 1:].reshape(-1, 1)
    l = torch.gather(logs, dim=1, index=index)

    return float(l.sum()), seq_len
    # return l.flatten().tolist(), seq_len



def calc_perplexity_v2(prompts, pre_idx=1):
    encs = [encode(p, add_special_tokens=False) for p in prompts]
    
    result = shared.model.call_perplexity(encs, pre_idx)
    
    return result


def get_model_info():
    return {
        'model_name': shared.model_name,
        'lora_names': shared.lora_names,
        # dump
        'shared.settings': shared.settings,
        'shared.args': vars(shared.args),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/v1/model':
            self.send_response(200)
            self.end_headers()
            response = json.dumps({
                'result': shared.model_name
            })

            self.wfile.write(response.encode('utf-8'))
        else:
            self.send_error(404)

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        body = json.loads(self.rfile.read(content_length).decode('utf-8'))

        if self.path == '/api/v1/generate':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            prompt = body['prompt']
            generate_params = build_parameters(body)
            stopping_strings = generate_params.pop('stopping_strings')
            generate_params['stream'] = False

            generator = generate_reply(
                prompt, generate_params, stopping_strings=stopping_strings, is_chat=False)

            answer = ''
            for a in generator:
                answer = a

            response = json.dumps({
                'results': [{
                    'text': answer
                }]
            })

            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/chat':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            user_input = body['user_input']
            regenerate = body.get('regenerate', False)
            _continue = body.get('_continue', False)

            generate_params = build_parameters(body, chat=True)
            generate_params['stream'] = False

            generator = generate_chat_reply(
                user_input, generate_params, regenerate=regenerate, _continue=_continue, loading_message=False)

            answer = generate_params['history']
            for a in generator:
                answer = a

            response = json.dumps({
                'results': [{
                    'history': answer
                }]
            })

            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/chateval_o':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            generate_params = build_parameters(body, chat=True)
            generate_params['stream'] = False

            prompt = generate_chat_prompt("", generate_params, regenerate=False, _continue=True, history=generate_params['history'])
            p, l = calc_perplexity_v1(prompt)

            ret = [dict(logit=p, len=l)]
            for choice in body.get('choices', []):
                history = deepcopy(generate_params['history'])
                history['internal'][-1][-1] += choice
                history['visible'][-1][-1] += choice

                p_new = generate_chat_prompt("", generate_params, regenerate=False, _continue=True, history=history)

                p, l = calc_perplexity_v1(p_new)
                ret.append(dict(logit=p, len=l))


            response = json.dumps({
                'ret': ret
            })

            self.wfile.write(response.encode('utf-8'))
        
        elif self.path == '/api/v1/chateval':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            generate_params = build_parameters(body, chat=True)
            generate_params['stream'] = False
            
            prompts = []

            p = generate_chat_prompt("", generate_params, regenerate=False, _continue=True, history=generate_params['history'])
            prompts.append(p)

            for choice in body.get('choices', []):
                history = deepcopy(generate_params['history'])
                history['internal'][-1][-1] += choice
                history['visible'][-1][-1] += choice

                p = generate_chat_prompt("", generate_params, regenerate=False, _continue=True, history=history)
                prompts.append(p)
            
            ret = calc_perplexity_v2(prompts)


            response = json.dumps({
                'ret': ret
            })

            self.wfile.write(response.encode('utf-8'))


        elif self.path == '/api/v1/stop-stream':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            stop_everything_event()

            response = json.dumps({
                'results': 'success'
            })

            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/model':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            # by default return the same as the GET interface
            result = shared.model_name

            # Actions: info, load, list, unload
            action = body.get('action', '')

            if action == 'load':
                model_name = body['model_name']
                args = body.get('args', {})
                print('args', args)
                for k in args:
                    setattr(shared.args, k, args[k])

                shared.model_name = model_name
                unload_model()

                model_settings = get_model_settings_from_yamls(shared.model_name)
                shared.settings.update(model_settings)
                update_model_parameters(model_settings, initial=True)

                if shared.settings['mode'] != 'instruct':
                    shared.settings['instruction_template'] = None

                try:
                    shared.model, shared.tokenizer = load_model(shared.model_name)
                    if shared.args.lora:
                        add_lora_to_model(shared.args.lora)  # list

                except Exception as e:
                    response = json.dumps({'error': {'message': repr(e)}})

                    self.wfile.write(response.encode('utf-8'))
                    raise e

                shared.args.model = shared.model_name

                result = get_model_info()

            elif action == 'unload':
                unload_model()
                shared.model_name = None
                shared.args.model = None
                result = get_model_info()

            elif action == 'list':
                result = get_available_models()

            elif action == 'info':
                result = get_model_info()

            response = json.dumps({
                'result': result,
            })

            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/token-count':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            tokens = encode(body['prompt'])[0]
            response = json.dumps({
                'results': [{
                    'tokens': len(tokens)
                }]
            })

            self.wfile.write(response.encode('utf-8'))
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        super().end_headers()


def _run_server(port: int, share: bool = False, tunnel_id=str):
    address = '0.0.0.0' if shared.args.listen else '127.0.0.1'

    server = ThreadingHTTPServer((address, port), Handler)

    def on_start(public_url: str):
        print(f'Starting non-streaming server at public url {public_url}/api')

    if share:
        try:
            try_start_cloudflared(port, tunnel_id, max_attempts=3, on_start=on_start)
        except Exception:
            pass
    else:
        print(
            f'Starting API at http://{address}:{port}/api')

    server.serve_forever()


def start_server(port: int, share: bool = False, tunnel_id=str):
    Thread(target=_run_server, args=[port, share, tunnel_id], daemon=True).start()
