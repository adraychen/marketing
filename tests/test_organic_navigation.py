import pytest
from streamlit.testing.v1 import AppTest
from frontend import content_marketing as ui


@pytest.mark.parametrize('source,title,tabs', [
    ('facebook', 'Facebook Organic', ['Posts', 'Post performance', 'Campaign links', 'Page analytics', 'UTM Link Builder']),
    ('shopify', 'Shopify Blog', ['Articles', 'Article performance', 'Campaign links', 'UTM Link Builder']),
])
def test_channel_controls_and_tabs_agree(monkeypatch, source, title, tabs):
    catalog = {'sources': [], 'campaigns': [{'slug': 'fb', 'name': 'Facebook campaign'}, {'slug': 'blog', 'name': 'Blog campaign'}],
               'items': [dict(id=s, source=s, title=s, campaigns=[c], active=True, published_at='2026-11-01')
                         for s,c in [('facebook','fb'), ('shopify','blog')]]}
    seen = []
    monkeypatch.setattr(ui, 'call', lambda *a, **k: catalog)
    monkeypatch.setattr(ui, 'show_visibility_summary', lambda *a: None)
    monkeypatch.setattr(ui, 'show_item_performance', lambda route, items: seen.extend(i['source'] for i in items))
    monkeypatch.setattr(ui, 'show_facebook_analytics', lambda route, items: seen.extend(i['source'] for i in items))
    monkeypatch.setattr(ui, 'show_utm_builder', lambda base, channel, items: seen.extend(i['source'] for i in items))
    app = AppTest.from_string(f"from frontend.content_marketing import show_content_marketing\nshow_content_marketing('/orgs/test', '{source}')").run()
    assert not app.exception
    assert app.title[0].value == title
    assert [t.label for t in app.tabs] == tabs
    assert set(seen) == {source}
    assert app.selectbox[0].options == (['All Facebook posts', 'Facebook campaign'] if source == 'facebook' else ['All blog articles', 'Blog campaign'])
