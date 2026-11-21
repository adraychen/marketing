from unittest.mock import MagicMock, patch
import httpx
import pytest
from frontend import client

class State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__

@pytest.fixture
def ui():
    mock = MagicMock()
    mock.session_state = State(token='old', refresh_token='refresh', expires=0)
    with patch.object(client, 'st', mock), patch.object(client.time, 'sleep'):
        yield mock

@pytest.mark.parametrize('code', [None, 429, 500, 502, 503, 504])
def test_temporary_refresh_failure_retains_session(ui, code):
    with patch.object(client, 'call', side_effect=client.APIError('temporary', code)):
        with pytest.raises(client.APIError, match='retained'):
            client.refresh()
    assert ui.session_state['token'] == 'old'
    assert ui.session_state['refresh_token'] == 'refresh'
    ui.rerun.assert_not_called()


def test_refresh_retries_temporary_http_failure(ui):
    with patch.object(client, 'call', side_effect=[client.APIError('temporary', 503),
                dict(access_token='new', refresh_token='rotated', expires_in=3600)]) as request:
        client.refresh()
    assert request.call_count == 2
    assert ui.session_state.token == 'new'
    assert ui.session_state.refresh_token == 'rotated'


def test_rejected_refresh_clears_session(ui):
    with patch.object(client, 'call', side_effect=client.APIError('expired', 401)):
        with pytest.raises(client.APIError):
            client.refresh()
    assert not ui.session_state
    ui.rerun.assert_called_once()


def test_api_401_refreshes_then_retries_once(ui):
    ui.session_state.expires = float('inf')
    responses = [httpx.Response(401, json={'detail': 'expired'}),
                 httpx.Response(200, json=dict(access_token='new', refresh_token='rotated', expires_in=3600)),
                 httpx.Response(200, json={'ok': True})]
    with patch.object(client, 'backend_url', return_value='https://example.test'), patch.object(client.httpx, 'request', side_effect=responses) as request:
        assert client.call('/orgs') == {'ok': True}
    assert request.call_count == 3
    assert request.call_args.kwargs['headers']['Authorization'] == 'Bearer new'
    ui.rerun.assert_not_called()
