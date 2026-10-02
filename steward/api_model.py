"""来源：本项目原创。OpenAI兼容Chat Completions适配器；凭据来自命名环境变量。
协议参考：https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

from .core import ContractError
from .codex_model import ModelUnavailable, schema_for, build_prompt
from .extensions import https_url, fetch_json


class ApiKeyUnavailable(ModelUnavailable):
    pass


def validate_connection(config):
    required = {'id', 'base_url', 'model', 'key_env', 'input_price', 'output_price', 'quality_note', 'enabled'}
    if not isinstance(config, dict) or set(config) != required:
        raise ContractError('API配置字段不完整')
    for key in ('id', 'key_env'):
        if not isinstance(config[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', config[key]):
            raise ContractError(f'{key}格式错误')
    if not isinstance(config['model'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}', config['model']):
        raise ContractError('供应商模型名称格式错误')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', config['key_env']):
        raise ContractError('请填写环境变量名称，而不是密钥本身')
    https_url(config['base_url'])
    for key in ('input_price', 'output_price'):
        value = config[key]
        if value is not None and (type(value) not in (int, float) or not 0 <= value <= 100000):
            raise ContractError('价格须为空或非负数，单位统一为美元/百万Token')
    if type(config['enabled']) is not bool or not isinstance(config['quality_note'], str) or len(config['quality_note']) > 1000:
        raise ContractError('启用状态或质量说明无效')
    return config


def request_completion(config, role, context, vault_dir=None):
    validate_connection(config)
    from .credentials import CredentialVault
    try:
        key = CredentialVault(vault_dir).read(config) if vault_dir else None
    except (ValueError, OSError):
        raise ApiKeyUnavailable('无法读取当前账户加密密钥，请重新保存') from None
    key = key or os.environ.get(config['key_env'])
    if not key:
        raise ApiKeyUnavailable('未找到密钥，请在API接入中保存密钥或配置环境变量')
    prompt = (build_prompt(role, {})+'\nJSON结构：'+json.dumps(schema_for(role), ensure_ascii=False)
              if role != 'connection_test' else '')
    if role == 'connection_test':
        prompt = '这是连接测试。仅返回JSON对象：{"content":"连接成功"}。不调用工具。'
    payload = {'model': config['model'], 'messages': [{'role': 'system', 'content': prompt},
               {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}],
               'response_format': {'type': 'json_object'}}
    data = fetch_json(config['base_url'].rstrip('/')+'/chat/completions',
                      data=json.dumps(payload).encode(), timeout=None,
                      headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'})
    choice = data['choices'][0]
    if choice.get('finish_reason') != 'stop' or choice['message'].get('tool_calls'):
        raise ModelUnavailable('API没有返回完整的文本结果')
    usage = data.get('usage', {})
    safe_usage = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                  if isinstance(usage, dict) and type(usage.get(key)) is int and usage[key] >= 0}
    return json.loads(choice['message']['content']), safe_usage


class ApiModel:
    mode = 'api'
    effort = 'provider_default'

    def __init__(self, config, control=None, vault_dir=None):
        self.config = validate_connection(config)
        self.name = 'api:'+config['id']
        self.control = control
        self.vault_dir = str(vault_dir) if vault_dir else None
        self.last_receipt = None

    def respond(self, role, context):
        from .runtime import RunControl
        control = self.control or RunControl()
        self.last_receipt = None
        started = time.monotonic()
        # 使用独立进程隔离网络等待，使停止操作可以结束本机请求；密钥不进入命令行或模型上下文。
        envelope = json.dumps({'config': self.config, 'role': role, 'context': context, 'vault_dir': self.vault_dir})
        if len(envelope.encode()) > 90000:
            raise ModelUnavailable('本轮API上下文过长')
        executable = Path(sys.executable)
        if executable.name.lower() == 'pythonw.exe':
            executable = executable.with_name('python.exe')
        result = control.run([str(executable), '-E', '-s', '-B', '-m', 'steward.api_model'], input=envelope,
                             cwd=str(Path(__file__).resolve().parent.parent), phase='负责人API处理中')
        try:
            value = json.loads(result.stdout)
            if result.returncode:
                raise ValueError()
        except (ValueError, KeyError):
            raise ModelUnavailable('API调用未完成，请检查连接地址、密钥环境变量、模型权限与兼容性') from None
        if not value.get('ok'):
            messages = {'auth': '供应商拒绝身份验证，请检查密钥或模型权限',
                        'quota': '供应商限流或额度不足，请检查账户后继续',
                        'network': '无法连接供应商，请检查网络与服务地址',
                        'request': '供应商不接受此模型或JSON协议，请核对模型名称与兼容性',
                        'server': '供应商服务暂时失败，已保留任务，请稍后继续',
                        'key': '未找到或无法解密密钥，请在API接入中重新保存',
                        'protocol': '供应商没有返回完整有效的JSON文本结果'}
            raise ModelUnavailable(messages.get(value.get('code'), messages['protocol']))
        self.last_receipt = {'provider': 'api', 'model': self.name, 'reasoning_effort': self.effort,
                             'role': role, 'elapsed_seconds': round(time.monotonic()-started, 2),
                             'usage': value['usage'], 'completed': True}
        usage = value['usage']
        prices = self.config
        if all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens')) and all(prices[k] is not None for k in ('input_price', 'output_price')):
            self.last_receipt['estimated_cost_usd'] = round((usage['prompt_tokens']*prices['input_price']+
                                                            usage['completion_tokens']*prices['output_price'])/1000000, 8)
            self.last_receipt['cost_note'] = '按用户填写价格估算，不含缓存差异、折扣、税费，以供应商账单为准'
        return value['result']


def main():
    try:
        message = json.loads(sys.stdin.read(100001))
        result, usage = request_completion(message['config'], message['role'], message['context'], message.get('vault_dir'))
        print(json.dumps({'ok': True, 'result': result, 'usage': usage}))
    except ApiKeyUnavailable:
        print(json.dumps({'ok': False, 'code': 'key'}))
    except HTTPError as error:
        code = ('auth' if error.code in (401, 403) else 'quota' if error.code == 429 else
                'server' if error.code >= 500 else 'request')
        print(json.dumps({'ok': False, 'code': code}))
    except (URLError, OSError):
        print(json.dumps({'ok': False, 'code': 'network'}))
    except Exception:
        # 供应商原始错误可能含凭据和请求，不回传原文。
        print(json.dumps({'ok': False, 'code': 'protocol'}))


if __name__ == '__main__':
    main()
