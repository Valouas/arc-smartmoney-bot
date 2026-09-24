"""Read-only consumer of the expiring historical paper-selection contract."""
import json
from pathlib import Path
from web3 import Web3
from wallet_policy import infrastructure_addresses
from .policy import decimal
from paper_admission import assess, POLICY, ZERO_ADDRESS


class SelectionError(ValueError): pass


def load_selection(path, cfg, now):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        review=(data['schema_version']==2 and data['purpose']=='paper_review_selection'
                and data.get('admission_policy')==POLICY)
        strict=(data['schema_version']==1 and data['purpose']=='historical_selection_for_paper')
        if (not (review or strict) or data['chain_id'] != 5042
                or data['live_signal_authority'] is not False
                or data['requires_consumer_validation'] is not True
                or data['one_wallet_per_identity_cluster'] is not (not review)
                or type(data['generated_at']) is not int or not 0 <= now-data['generated_at'] <= cfg.historical_refresh_seconds
                or not isinstance(data['wallets'], list)):
            raise ValueError()
        excluded = infrastructure_addresses(cfg)
        result, clusters = {}, set()
        for entry in data['wallets']:
            wallet = Web3.to_checksum_address(entry['wallet']).lower()
            cluster = Web3.to_checksum_address(entry['identity_cluster_id']).lower()
            if (wallet==ZERO_ADDRESS or cluster==ZERO_ADDRESS or wallet in excluded or wallet in result
                    or entry['live_signal_authority'] is not False
                    or entry['infrastructure_excluded'] is not False
                    or entry['snapshot_phase'] != 'COMPLETE'
                    or type(entry['snapshot_at']) is not int
                    or not 0 <= now-entry['snapshot_at'] <= cfg.historical_max_age_hours*3600
                    or type(entry['expires_at']) is not int or entry['expires_at'] <= now
                    or entry['expires_at'] > min(data['generated_at']+cfg.historical_refresh_seconds,
                                                 entry['snapshot_at']+cfg.historical_max_age_hours*3600)
                    or not isinstance(entry['buy_details'], list)):
                raise ValueError()
            if review:
                warnings,blockers=assess(entry,cfg,now)
                if (entry['paper_review_eligible'] is not True or blockers
                        or entry['paper_review_warnings']!=warnings or entry['paper_review_blockers']):
                    raise ValueError()
            else:
                warnings=[]
                if (cluster in clusters or entry['historical_class']!='HISTORICALLY_QUALIFIED'
                        or entry['paper_selection_eligible'] is not True or entry['identity_eligible'] is not True
                        or entry['data_complete'] is not True or entry['blocking_reasons']
                        or entry.get('funder')==ZERO_ADDRESS or entry.get('null_funder_identity_unknown')):
                    raise ValueError()
            baseline = []
            for buy in entry['buy_details']:
                if type(buy['timestamp']) is not int or not 0 <= buy['timestamp'] <= entry['snapshot_at']:
                    raise ValueError()
                amount = decimal(buy['buy_usdc'])
                if amount <= 0: raise ValueError()
                baseline.append(str(amount))
            if len(baseline) < cfg.promotion_min_launch_buys: raise ValueError()
            if review and len(baseline)!=entry['buys']: raise ValueError()
            result[wallet] = {'baseline': baseline, 'cluster': cluster,
                              'snapshot_at': entry['snapshot_at'], 'expires_at': entry['expires_at'],
                              'admission':'PAPER_REVIEW' if review else 'HISTORICALLY_QUALIFIED',
                              'historical_class':entry['historical_class'],'warnings':warnings,
                              'data_complete':entry['data_complete'],'identity_known':entry['identity_eligible'],
                              'related_wallets_count':entry.get('related_wallets_count',0)}
            clusters.add(cluster)
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise SelectionError('Selection missing, expired or invalid; new buys disabled') from None
