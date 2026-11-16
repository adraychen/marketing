from datetime import date

from frontend.meta_daily_charts import daily_figure, metric_value


def test_charts_keep_ads_separate_dates_scoped_and_missing_days_gapped():
    data = {'campaigns': [{'id': 'c', 'name': 'Campaign'}], 'ads': [], 'daily': [
        {'ad_id': 'a', 'campaign_id': 'c', 'day': '2026-11-01', 'spend': 5, 'website_purchases': 0},
        {'ad_id': 'a', 'campaign_id': 'c', 'day': '2026-11-03', 'spend': 10, 'website_purchases': 1},
        {'ad_id': 'b', 'campaign_id': 'c', 'day': '2026-11-03', 'spend': 20, 'website_purchases': 2},
        {'ad_id': 'a', 'campaign_id': 'c', 'day': '2026-11-04', 'spend': 99}]}
    fig = daily_figure(data, ['a', 'b'], 'Website purchases', date(2026, 11, 1), date(2026, 11, 3), 'CAD')
    assert list(fig.data[0].y) == [0, None, 1]
    assert list(fig.data[1].y) == [None, None, 2]
    assert fig.data[0].connectgaps is False
    assert len(fig.data[0].x) == 3


def test_chart_ratios_and_actions_do_not_infer_missing_purchases():
    row = {'spend': 102.28, 'website_purchases': 1, 'website_purchase_value': 314.50,
           'actions': [{'action_type': 'view_content', 'value': '99'},
                       {'action_type': 'offsite_conversion.fb_pixel_view_content', 'value': '5'}]}
    assert metric_value(row, 'Cost per purchase') == 102.28
    assert metric_value(row, 'ROAS') == 314.50 / 102.28
    assert metric_value(row, 'ViewContent') == 5
    assert metric_value({}, 'Website purchases') is None
    assert metric_value({'spend': 10, 'website_purchases': 0}, 'Cost per purchase') is None


def test_daily_charts_render_and_metric_selection_changes_charts():
    from streamlit.testing.v1 import AppTest
    def panel():
        from datetime import date
        from frontend.meta_daily_charts import show_daily_charts
        data = {'campaigns': [{'id': 'c', 'name': 'Campaign'}], 'ads': [], 'daily': [
            {'ad_id': 'a', 'campaign_id': 'c', 'day': '2026-11-14', 'spend': 102.28,
             'inline_link_clicks': 6, 'website_purchases': 1, 'website_purchase_value': 314.5}]}
        show_daily_charts(data, date(2026, 11, 1), date(2026, 11, 14), 'CAD')
    app = AppTest.from_function(panel).run()
    assert not app.exception
    assert len(app.get('plotly_chart')) == 4
    app.multiselect[1].set_value(['ROAS']).run()
    assert not app.exception
    assert len(app.get('plotly_chart')) == 1
