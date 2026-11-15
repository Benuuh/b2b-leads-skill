"""Deterministic customer-nature classification (spec P0-4)."""
from app.rules.customer_type import CUSTOMER_TYPES, classify_customer_type


def label(name='', business_models=(), text=''):
    return classify_customer_type(name=name, business_models=business_models, text=text)['type']


def test_every_label_is_in_the_declared_set():
    assert CUSTOMER_TYPES == ('租赁/活动', '制造/工厂', '批发/进口', '零售/门店', '园艺中心',
                              '泳池/庭院服务', '电商', '设计/安装', '其他')
    for name in ('Sunbelt Party Rental', 'Acme Manufacturing', 'Garden Center', 'Patio',
                 'Blue Pool Service', 'Online Store', 'Design Studio', 'XYZ Trading', 'Nothing'):
        assert label(name) in CUSTOMER_TYPES


def test_acceptance_names_map_to_the_expected_nature():
    assert label('Sunbelt Party Rental') == '租赁/活动'
    assert label('Acme Manufacturing') == '制造/工厂'
    assert label('Green Thumb Garden Center') == '园艺中心'
    assert label('Patio Furniture Store') == '零售/门店'
    assert label('Patio') == '零售/门店'


def test_observed_business_models_win_over_the_name():
    assert label('Sunbelt Party Rental', business_models=['manufacturer']) == '制造/工厂'
    assert label('Patio', business_models=['distributor']) == '批发/进口'
    assert label('Patio', business_models=['retailer']) == '零售/门店'
    assert label('Patio', business_models=['service_provider']) == '设计/安装'
    # Synonyms are normalized through the registered business-model vocabulary.
    assert label('Patio', business_models=['Manufactures']) == '制造/工厂'
    result = classify_customer_type(name='Patio', business_models=['manufacturer'])
    assert result['basis'] == 'business_models'


def test_specific_trades_are_read_before_generic_words_and_text_is_used():
    assert label('Patio Furniture Manufacturing') == '制造/工厂'
    assert label('Garden Center Supply') == '园艺中心'
    assert label('Acme', text='we run a party rental service') == '租赁/活动'
    assert label('Acme', text='wholesale distributor of outdoor furniture') == '批发/进口'
    assert label('Acme') == '其他'
    assert classify_customer_type(name='Acme')['basis'] == 'none'


def test_classification_is_case_insensitive_and_repeatable():
    first = classify_customer_type(name='SUNBELT PARTY RENTAL')
    second = classify_customer_type(name='sunbelt party rental')
    assert first == second and first['type'] == '租赁/活动'
    assert first['matched'] == 'party rental'
