"""Muestra prospectiva de descartes y selecciones: cotizaciones, nunca operaciones."""
import hashlib
import json
import statistics
import time

from providers import USDC, timestamp
from version import SCANNER_VERSION

STUDY = {'id':'forward-10usdc-v1', 'amount_usdc':10, 'slippage_bps_per_side':50,
         'fixed_cost_usdc_per_side':0.05, 'horizons_hours':[1,2,4],
         'deadline_seconds':180, 'sample_modulus':5, 'daily_sample_cap':24}
SIGNAL_STUDY_ID = 'forward-10usdc-confirmed-v1'


def create_study(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS research_entries_v8 (
            id INTEGER PRIMARY KEY, study TEXT NOT NULL, scanner_version TEXT NOT NULL,
            plan_hash TEXT NOT NULL, mint TEXT NOT NULL, observation_id INTEGER NOT NULL REFERENCES observations(id),
            sampled_at REAL NOT NULL, quoted_at REAL, status TEXT NOT NULL, quantity_raw TEXT,
            cost_usdc REAL NOT NULL, labels TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
            UNIQUE(study,scanner_version,plan_hash,mint)
        );
        CREATE TABLE IF NOT EXISTS research_outcomes_v8 (
            entry_id INTEGER NOT NULL REFERENCES research_entries_v8(id), horizon INTEGER NOT NULL,
            due_at REAL NOT NULL, deadline REAL NOT NULL, status TEXT NOT NULL,
            checked_at REAL, return_pct REAL, detail TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(entry_id,horizon)
        );
        CREATE INDEX IF NOT EXISTS research_due ON research_outcomes_v8(status,due_at);
    ''')


class ForwardStudy:
    def __init__(self, store):
        self.db = store.db

    @staticmethod
    def sampled(mint, plan_hash):
        identity = ':'.join((STUDY['id'],SCANNER_VERSION,plan_hash,mint))
        return int(hashlib.sha256(identity.encode()).hexdigest(),16) % STUDY['sample_modulus'] == 0

    def enroll(self, row, observation_id, jupiter, clock=time.time):
        mint, plan = row['base_address'], row.get('profile_plan_hash')
        if (row.get('scanner_version')!=SCANNER_VERSION or not plan or not row.get('pair_address')
                or row.get('lifecycle',{}).get('phase') not in ('established','new_pool','recent_migration')
                or not self.sampled(mint,plan)):
            return False
        at = clock()
        labels = {ident:{'selected':bool(p['quality_pass']), 'state':p['state'],
                          'checks':p['decision_checks']} for ident,p in row.get('profiles',{}).items()}
        study_id = STUDY['id']
        prior = self.db.execute('''SELECT labels FROM research_entries_v8
            WHERE study=? AND scanner_version=? AND plan_hash=? AND mint=?''',
            (study_id,SCANNER_VERSION,plan,mint)).fetchone()
        if prior:
            # No perder una señal confirmada porque el primer snapshot aún esperaba trayectoria.
            # Mantener intactas las etiquetas iniciales: nunca usar el futuro para reclasificarlas.
            if (not any(label['selected'] for label in labels.values())
                    or any(label['selected'] for label in json.loads(prior['labels']).values())):
                return False
            study_id = SIGNAL_STUDY_ID
        # Registrar antes de consultar: los fallos no desaparecen de la muestra ni se reintentan como nuevas entradas.
        with self.db:
            count = self.db.execute('SELECT count(*) FROM research_entries_v8 WHERE sampled_at>=?',
                                   (at-at%86400,)).fetchone()[0]
            if count >= STUDY['daily_sample_cap']:
                return False
            cursor = self.db.execute('''INSERT OR IGNORE INTO research_entries_v8
                (study,scanner_version,plan_hash,mint,observation_id,sampled_at,status,cost_usdc,labels)
                VALUES (?,?,?,?,?,?,'unavailable',?,?)''',
                (study_id,SCANNER_VERSION,plan,mint,observation_id,at,
                 STUDY['amount_usdc']+STUDY['fixed_cost_usdc_per_side'],json.dumps(labels,ensure_ascii=False)))
            if not cursor.rowcount:
                return False
            entry_id = cursor.lastrowid
            for horizon in STUDY['horizons_hours']:
                due = at+horizon*3600
                self.db.execute('INSERT INTO research_outcomes_v8(entry_id,horizon,due_at,deadline,status) VALUES (?,?,?,?,?)',
                    (entry_id,horizon,due,due+STUDY['deadline_seconds'],'untrackable'))
        state, quantity, quoted_at, detail = 'unavailable', None, None, {'configuration':{**STUDY,'id':study_id}}
        try:
            observed = timestamp(row.get('scanned_at'))
            if observed is None or not 0 <= at-observed <= 60:
                raise RuntimeError('observación inicial caducada')
            buy = jupiter.quote(USDC,mint,str(STUDY['amount_usdc']*1000000))
            quantity = int(buy['out_amount'])*(10000-STUDY['slippage_bps_per_side'])//10000
            if quantity <= 0:
                raise RuntimeError('cantidad simulada insuficiente')
            sell = jupiter.quote(mint,USDC,str(quantity))
            quoted_at = clock()
            if any(not 0 <= quoted_at-q['received_at'] <= 30 for q in (buy,sell)):
                raise RuntimeError('cotización inicial caducada')
            state = 'quoted'
            detail.update(buy=buy,initial_exit=sell)
        except RuntimeError as exc:
            detail['reason'] = str(exc)
        with self.db:
            self.db.execute('''UPDATE research_entries_v8 SET status=?,quantity_raw=?,quoted_at=?,detail=? WHERE id=?''',
                            (state,str(quantity) if quantity else None,quoted_at,json.dumps(detail),entry_id))
            for horizon in STUDY['horizons_hours']:
                due = (quoted_at if state=='quoted' else at)+horizon*3600
                self.db.execute('UPDATE research_outcomes_v8 SET due_at=?,deadline=?,status=? WHERE entry_id=? AND horizon=?',
                    (due,due+STUDY['deadline_seconds'],'pending' if state=='quoted' else 'untrackable',entry_id,horizon))
        return True

    def evaluate(self, jupiter, clock=time.time, limit=2):
        at = clock()
        rows = self.db.execute('''SELECT o.*,e.mint,e.quantity_raw,e.cost_usdc,e.study
            FROM research_outcomes_v8 o JOIN research_entries_v8 e ON e.id=o.entry_id
            WHERE o.status='pending' AND o.due_at<=? AND (o.checked_at IS NULL OR o.checked_at<=? OR o.deadline<?)
            ORDER BY o.deadline,o.entry_id,o.horizon LIMIT ?''', (at,at-60,at,limit)).fetchall()
        for row in rows:
            at, status, result = clock(), 'pending', None
            detail = json.loads(row['detail'])
            if row['study'] not in (STUDY['id'],SIGNAL_STUDY_ID):
                status, detail = 'untrackable', {'reason':'versión del estudio no soportada'}
            elif at > row['deadline']:
                status = 'unavailable' if detail.get('reason') else 'missed'
            else:
                try:
                    quote = jupiter.quote(row['mint'],USDC,row['quantity_raw'])
                    at = clock()
                    if at > row['deadline']:
                        status, detail = 'missed', {'reason':'respuesta posterior al plazo'}
                    elif not 0 <= at-quote['received_at'] <= 30:
                        detail = {'reason':'cotización de salida caducada'}
                    else:
                        net = max(0,int(quote['out_amount'])/1e6*(1-STUDY['slippage_bps_per_side']/10000)
                                  -STUDY['fixed_cost_usdc_per_side'])
                        result = (net/row['cost_usdc']-1)*100
                        status, detail = 'quoted', {'quote':quote,'net_usdc_after_assumptions':net}
                except RuntimeError as exc:
                    detail = {'reason':str(exc)}
            with self.db:
                self.db.execute('''UPDATE research_outcomes_v8 SET status=?,checked_at=?,return_pct=?,detail=?
                    WHERE entry_id=? AND horizon=? AND status='pending' ''',
                    (status,at,result,json.dumps(detail),row['entry_id'],row['horizon']))

    def report(self, now=None):
        now = time.time() if now is None else now
        rows = self.db.execute('''SELECT o.*,e.scanner_version,e.plan_hash,e.study,e.labels FROM research_outcomes_v8 o
            JOIN research_entries_v8 e ON e.id=o.entry_id''').fetchall()
        buckets = {}
        for row in rows:
            labels = json.loads(row['labels'])
            selectors = [('all',None)] + [(name,label['selected']) for name,label in labels.items()]
            for name,selected in selectors:
                key = (row['study'],row['scanner_version'],row['plan_hash'],row['horizon'],name,selected)
                buckets.setdefault(key,[]).append(row)
        groups = []
        for key,items in sorted(buckets.items()):
            states, returns = {}, []
            due = 0
            for item in items:
                status = item['status']
                if status=='pending' and item['deadline']<now:
                    status = 'unavailable' if json.loads(item['detail']).get('reason') else 'missed'
                states[status] = states.get(status,0)+1
                due += item['due_at']<=now
                if status=='quoted':
                    returns.append(item['return_pct'])
            groups.append(dict(zip(('study','scanner_version','plan_hash','horizon_hours','selector','selected'),key),
                samples=len(items), due=due, states=states, coverage_pct=len(returns)/due*100 if due else None,
                mean_return_pct_observed_only=statistics.mean(returns) if returns else None,
                median_return_pct_observed_only=statistics.median(returns) if returns else None))
        return {'configuration':STUDY, 'confirmed_study_id':SIGNAL_STUDY_ID,
                'groups':groups, 'profitability_proven':False,
                'limitations':['Muestra determinista del universo analizado; máximo 24 entradas/día. No es todo el mercado.',
                    'Muestra inicial y primera confirmación posterior en estudios separados, con cuota diaria compartida.',
                    'Etiquetas iniciales inmutables; las cohortes pueden compartir monedas y no son independientes.',
                    'Tamaño común de 10 USDC. No son resultados de las carteras ni pruebas de stops o ventas parciales.',
                    'Comisiones y deslizamiento asumidos; cotizaciones sin ejecutar, MEV e impacto propio incompletos.',
                    'Los fallos y plazos vencidos se cuentan; las medias solo incluyen salidas observadas.',
                    'Etiquetas por perfil no son asignación aleatoria ni estiman causalidad de cada filtro.']}
