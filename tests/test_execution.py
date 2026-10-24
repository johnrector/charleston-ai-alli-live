import pytest
from adapters.mac_executor import executor_arguments, executor_environment
from alli_core.execution import Execution


def test_native_environment_definitions_keep_local_plugins_on_mac():
    local = Execution('self_hosted', '/Users/owner/Alli/work/task', ('/Users/owner/plugins/publisher',))
    assert local.configuration() == {'type':'self_hosted','workspace_directory':'/Users/owner/Alli/work/task',
                                      'capability_directories':['/Users/owner/plugins/publisher']}
    assert Execution().configuration() == {'type':'none'}
    assert Execution('openai_hosted', desktop=True).configuration()['desktop'] == {'enabled':True}
    with pytest.raises(ValueError): Execution('none', workspace='/private').configuration()
    with pytest.raises(ValueError): Execution('openai_hosted', plugin_roots=('/private',)).configuration()
    with pytest.raises(ValueError): Execution('self_hosted', '/Users/owner/../other').configuration()


def test_executor_connects_outbound_to_documented_openai_host_without_a_shell():
    url = 'wss://codex-cloud-environments.chatgpt.com/native/session?nonce=opaque'
    args = executor_arguments('/Applications/Codex CLI/codex', environment_id='env_one', remote_url=url)
    assert args == ['/Applications/Codex CLI/codex','exec-server','--remote',url,'--environment-id','env_one']
    assert '--listen' not in args
    assert executor_arguments('codex',environment_id='env_one',remote_url='https://api.openai.com')[3]=='https://api.openai.com'
    for endpoint in ['http://127.0.0.1','https://api.openai.com.evil.test','wss://codex-cloud-environments.chatgpt.com.evil.test',
                     'wss://secret@codex-cloud-environments.chatgpt.com','wss://codex-cloud-environments.chatgpt.com:444']:
        with pytest.raises(ValueError): executor_arguments('codex', environment_id='env_one', remote_url=endpoint)


def test_executor_never_inherits_application_credentials():
    env = executor_environment({'HOME':'/Users/owner','PATH':'/bin','OPENAI_API_KEY':'application-secret',
        'GOOGLE_CLIENT_SECRET':'google-secret','TWILIO_AUTH_TOKEN':'carrier-secret','DATABASE_URL':'private-db'},
        restricted_key='environment-only-key')
    assert env == {'HOME':'/Users/owner','PATH':'/bin','CODEX_API_KEY':'environment-only-key'}
    with pytest.raises(ValueError): executor_environment({'OPENAI_API_KEY':'same-key'}, restricted_key='same-key')
    with pytest.raises(ValueError): executor_environment({}, restricted_key='')
