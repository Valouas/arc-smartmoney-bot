"""Exploratory paper admission; strict historical qualification is unchanged."""
import math
from historical_metrics import performance_reasons

ZERO_ADDRESS='0x'+'0'*40
POLICY='paper_review_v2'
DEFAULT_SELECTION='data/paper_review_selection.json'
HISTORICAL_WARNINGS=frozenset({
    'external_or_unexplained_token_movement',
    'gas_cost_unknown',
    'unsupported_or_ambiguous_reconstruction',
    'insufficient_independent_profitable_tokens',
    'profitable_deployer_identity_unknown',
})
PERFORMANCE_WARNINGS=frozenset({
    'profit_concentrated_in_one_position',
    'regularity_not_established',
})


def assess(row,cfg,now):
    """Return explicit warnings/blockers; never make an unknown error admissible."""
    if not isinstance(row['blocking_reasons'],list) or any(not isinstance(x,str) for x in row['blocking_reasons']):
        raise ValueError('Malformed historical blockers')
    reasons=set(row['blocking_reasons'])
    warnings=reasons & (HISTORICAL_WARNINGS | PERFORMANCE_WARNINGS)
    blockers=reasons-(HISTORICAL_WARNINGS | PERFORMANCE_WARNINGS)
    null_identity=(row.get('null_funder_identity_unknown') is True or row.get('funder')==ZERO_ADDRESS)
    if null_identity and not row.get('shared_service'):
        # Correcting the old zero-address bug makes identity unknown, not known
        # independent. Only this specific identity defect is allowed in paper.
        blockers.discard('identity_unknown_or_shared_service')
        warnings.add('null_funder_identity_unknown')
    elif row.get('identity_eligible') is not True:
        blockers.add('identity_unknown_or_shared_service')
    if row.get('shared_service'): blockers.add('shared_service_excluded')
    if row.get('infrastructure_excluded') is not False: blockers.add('dex_infrastructure_excluded')
    if row.get('historical_class') not in ('WATCH','PROMISING','HISTORICALLY_QUALIFIED'):
        blockers.add('excluded_wallet')
    if (row.get('analysis_status')!='ANALYZED' or row.get('snapshot_phase')!='COMPLETE'
            or type(row.get('transactions_pending')) is not int or row['transactions_pending']!=0):
        blockers.add('historical_snapshot_incomplete')
    if row.get('collection_error'): blockers.add('historical_collection_error')
    if type(row.get('snapshot_at')) is not int or not 0<=now-row['snapshot_at']<=cfg.historical_max_age_hours*3600:
        blockers.add('historical_snapshot_stale')
    # Recalculate all strict performance rules at the consumer. Only the two
    # explicit exploratory exceptions become warnings, never extra authority.
    try:
        for key in ('buys','closed_positions','profitable_periods','closed_evidence_span_seconds'):
            if type(row[key]) is not int or row[key]<0: raise ValueError()
        for key in ('conservative_pnl_usdc','largest_position_contribution'):
            if isinstance(row[key],bool) or not isinstance(row[key],(float,int)) or not math.isfinite(row[key]):
                raise ValueError()
        if not 0<=row['largest_position_contribution']<=1: raise ValueError()
        if not isinstance(row['profitable_tokens'],list): raise ValueError()
        metrics=dict(row)
        metrics['profitable_tokens']=set(row['profitable_tokens'])
        metrics['concentration_risk']='HIGH' if row['largest_position_contribution']>cfg.historical_max_concentration else 'LOW'
        performance=set(performance_reasons(metrics,cfg))
        warnings.update(performance & PERFORMANCE_WARNINGS)
        blockers.update(performance-PERFORMANCE_WARNINGS)
        if not isinstance(row['issues'],list) or any(not isinstance(x,str) for x in row['issues']): raise ValueError()
        if set(row['issues'])-HISTORICAL_WARNINGS:
            blockers.update(set(row['issues'])-HISTORICAL_WARNINGS)
        warnings.update(set(row['issues']) & HISTORICAL_WARNINGS)
    except (ValueError,TypeError,KeyError,OverflowError):
        blockers.add('invalid_performance_data')
    return sorted(warnings),sorted(blockers)
