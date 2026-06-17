from __future__ import annotations

import csv, json, subprocess, sys
from pathlib import Path

from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus

ROOT = Path(__file__).resolve().parents[1]

def run_cli(*args: str):
    return subprocess.run([sys.executable, '-m', 'accountant_copilot.cli', *args], cwd=ROOT, env={'PYTHONPATH':'src'}, text=True, capture_output=True, check=False)

def state(path: Path, **kw):
    path.write_text(EngagementState(engagement_id='e1', entity_name='XYZ Trust', entity_type='discretionary_trust', fy_start='2024-07-01', fy_end='2025-06-30', documents_ref='docs', coa_ref='coa', **kw).model_dump_json())

def csvfile(path: Path, rows):
    with path.open('w', newline='') as h:
        w=csv.DictWriter(h, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

def test_ingest_validates_required_columns_and_normalises_values(tmp_path: Path):
    sp=tmp_path/'state.json'; bad=tmp_path/'bad.csv'; good=tmp_path/'good.csv'; state(sp)
    csvfile(bad, [{'description':'x','amount':'$1,200.50'}])
    failed=run_cli('ingest-source-document','--state',str(sp),'--document-id','doc_bad','--file-path',str(bad),'--document-type','bank_statement','--entity','XYZ','--period-start','2025-01-01','--period-end','2025-01-31')
    assert failed.returncode == 2
    assert 'missing required columns' in failed.stderr.lower()
    csvfile(good, [{'date':'10/01/2025','description':'Dividend','amount':'$1,200.50'}, {'date':'10/01/2025','description':'Dividend','amount':'$1,200.50'}])
    ok=run_cli('ingest-source-document','--state',str(sp),'--document-id','doc_good','--file-path',str(good),'--document-type','bank_statement','--entity','XYZ','--period-start','2025-01-01','--period-end','2025-01-31')
    assert ok.returncode == 1
    data=json.loads(sp.read_text())
    ev=data['evidence'][0]
    assert ev['amount']=='1200.50'
    assert ev['date']=='2025-01-10'
    assert any(e['category']=='duplicate_source_row' for e in data['exceptions'])

def test_matching_v2_tolerance_reference_and_composite(tmp_path: Path):
    sp=tmp_path/'state.json'; bank=tmp_path/'bank.csv'; events=tmp_path/'events.csv'; out=tmp_path/'matches.json'; state(sp)
    csvfile(bank,[{'date':'2025-01-10','description':'Dividend REF123','amount':'100.01'}, {'date':'2025-01-20','description':'Distribution batch','amount':'300.00'}])
    csvfile(events,[{'date':'2025-01-11','description':'Dividend support REF123','amount':'100.00'}, {'date':'2025-01-20','description':'Distribution A','amount':'100.00'}, {'date':'2025-01-20','description':'Distribution B','amount':'200.00'}])
    res=run_cli('match-transactions','--state',str(sp),'--bank-csv',str(bank),'--events-csv',str(events),'--output',str(out),'--amount-tolerance','0.02','--date-window-days','2')
    assert res.returncode == 0, res.stderr
    payload=json.loads(out.read_text())
    types=[m['match_type'] for m in payload['matches']]
    assert 'reference_date_amount_tolerance' in types
    assert 'composite_amount' in types
    assert all('evidence_refs' in m for m in payload['matches'])

def test_import_trial_balance_creates_coa_and_flags_suspense(tmp_path: Path):
    sp=tmp_path/'state.json'; tb=tmp_path/'tb.csv'; state(sp)
    csvfile(tb,[{'code':'1000','name':'Cash','type':'asset','presentation_group':'Current assets','balance':'1000'}, {'code':'9999','name':'Suspense','type':'asset','presentation_group':'Suspense','balance':'50'}])
    res=run_cli('import-trial-balance','--state',str(sp),'--trial-balance-csv',str(tb))
    assert res.returncode == 1
    data=json.loads(sp.read_text())
    assert any(a['code']=='1000' for a in data['chart_accounts'])
    assert any(e['category']=='suspense_account' for e in data['exceptions'])
    assert data['coa_review_required'] is True

def test_review_ui_and_statement_package_and_ci_quality(tmp_path: Path):
    sp=tmp_path/'state.json'; html=tmp_path/'review.html'; outdir=tmp_path/'pkg'
    state(sp, decisions=[AccountantDecision(decision_id='decision_final_signoff_0001', question='release?', selected_option='final_signoff', rationale='ok', status=DecisionStatus.APPROVED, approved_by='A')])
    ui=run_cli('export-review-ui','--state',str(sp),'--output',str(html))
    assert ui.returncode==0
    txt=html.read_text()
    assert 'coa_decisions' in txt and 'adjustment_decisions' in txt and 'preference_decisions' in txt and 'output_verifier_decisions' in txt
    fs=run_cli('render-statement-package','--state',str(sp),'--output-dir',str(outdir))
    assert fs.returncode==0
    assert (outdir/'balance_sheet.md').exists() and (outdir/'verifier_result.json').exists()
    assert 'demo smoke' in (ROOT/'.github/workflows/test.yml').read_text().lower()
    assert 'terminology scan' in (ROOT/'.github/workflows/test.yml').read_text().lower()
