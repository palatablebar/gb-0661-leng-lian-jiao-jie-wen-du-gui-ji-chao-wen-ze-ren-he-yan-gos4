def test_all_pages(client):
    for slug in ['intake','contract','model','calculate','quarantine','reconcile','closing','batches']:
        r=client.get('/'+slug);assert r.status_code==200;assert '<nav>' in r.text;assert 'statusbar' in r.text

def test_all_module_trials(client):
    for slug in ['intake','contract','model','calculate','quarantine','reconcile']:
        assert client.post('/api/'+slug,json={'sample':'dirty'}).status_code==200

def test_export_matches(client):
    q=client.get('/api/quarantine').json();csv=client.get('/api/quarantine/export');assert csv.status_code==200;assert csv.text.count('\n')==q['total']+1

def test_empty_input(client):
    r=client.post('/api/runs',json={'csv_text':''});assert r.status_code==200;assert r.json()['intake']['state']=='EMPTY_FILE'

def test_closing_page_shows_three_buckets(client):
    html=client.get('/closing').text
    assert '可证实分钟' in html and '未知分钟' in html and '未覆盖分钟' in html
    assert '正式账' in html and '预演' in html

def test_closing_api_round_trip(client):
    day=client.get('/api/closing/days').json()['days'][0]
    draft=client.get('/api/closing/draft?day='+day).json()
    assert draft['checks']['unknown_counts_as_compliant'] is False
    published=client.post('/api/closing/publish',json={'day':day}).json()
    assert published['totals']==draft['totals']
    assert client.post('/api/closing/publish',json={'day':day}).json()['idempotent'] is True
    assert client.get('/api/closing/verify?day='+day).json()['official_totals']==draft['totals']
