from sqlalchemy import select,func
from .models import Accession,Cabinet,Isolation,RetestPlan,RetestCandidate,now
from .common import get,reject,finish,audit
from .catalog_service import latest_observation
from .algorithm import cross_grade_probability
DEFAULT_LIMIT=5
def _today_plan(db,lock=False):
    stmt=select(RetestPlan).where(RetestPlan.plan_date==now().date())
    if lock:stmt=stmt.with_for_update().execution_options(populate_existing=True)
    plan=db.scalar(stmt)
    if not plan:
        plan=RetestPlan(daily_limit=DEFAULT_LIMIT);db.add(plan);db.flush()
        if lock:plan=db.scalar(select(RetestPlan).where(RetestPlan.id==plan.id).with_for_update().execution_options(populate_existing=True))
    return plan
def _eligible_folios(db):
    # 候选口径：当前仍在柜、且有病害观测的卷册；出柜与待入柜卷册不进当天名单。
    rows=db.execute(select(Accession,Isolation).join(Isolation,Isolation.accession_id==Accession.id).where(Isolation.active==True)).all()
    out=[]
    for folio,iso in rows:
        obs=latest_observation(db,folio.id)
        if obs:out.append((folio,iso,obs))
    return out
def _build_candidates(db,plan):
    # 重跑当天名单：已领取与已失效是终态记录，保留可追溯；只清除并重建未领且有效的旧推荐。
    keep=db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.claimed==True)).all()
    kept={c.accession_id:c for c in keep}
    # 已失效记录同属当天终态，保留追溯并阻止同册再次入榜。
    terminal=set(kept)|{c.accession_id for c in db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.invalid==True)).all()}
    stale=db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.claimed==False,RetestCandidate.invalid==False)).all()
    for old in stale:db.delete(old)
    db.flush()
    built={}
    for folio,iso,obs in _eligible_folios(db):
        if folio.id in terminal:continue
        prob=cross_grade_probability(obs.humidity,obs.holes,obs.mold)
        cabinet=db.get(Cabinet,iso.cabinet_id)
        row=RetestCandidate(plan_id=plan.id,accession_id=folio.id,cabinet_id=iso.cabinet_id,observation_id=obs.id,probability=prob['cross_grade_probability'],base_level=prob['base'],reason={'cabinet':cabinet.code if cabinet else '未入柜','humidity':obs.humidity,'holes':obs.holes,'mold':obs.mold,'swing':prob['swing'],'grid_points':prob['grid_points'],'level_probabilities':prob['level_probabilities'],'base_level':prob['base']})
        db.add(row);built[folio.id]=row
    db.flush()
    return kept,built
def _allocate(db,plan,kept,built):
    # 已领名额照算：入选总数（含已领取）不得超过当天上限。
    claimed_count=len(kept)
    open_rows=[c for c in built.values()]
    open_rows.sort(key=lambda c:(-c.probability,c.cabinet_id,c.accession_id))
    remaining=plan.daily_limit-claimed_count
    # 第一轮：每个柜按本柜最高跨级概率争一个名额（柜内已领册视同该柜已有席位）。
    seen_cabinets={c.cabinet_id for c in kept.values()}
    for c in open_rows:
        if remaining<=0:break
        if c.selected or c.cabinet_id in seen_cabinets:continue
        c.selected=True;c.cabinet_seat=True;seen_cabinets.add(c.cabinet_id);remaining-=1
    # 第二轮：剩余名额全馆按概率统一排序，先到先得。
    for c in open_rows:
        if remaining<=0:break
        if not c.selected:c.selected=True;remaining-=1
    for c in open_rows:
        if c.selected:
            seat='柜内最高概率保一名额' if c.cabinet_seat else '全馆概率排序入选'
            reason=dict(c.reason);reason['selection']=seat;reason['probability_text']=f"跨级概率 {c.probability:.2%}";c.reason=reason
        else:
            reason=dict(c.reason);reason['selection']='未入选：当天名额已排满';c.reason=reason
def rebuild(db,user=None,action='重跑当天复测名单'):
    plan=_today_plan(db,True)
    kept,built=_build_candidates(db,plan)
    _allocate(db,plan,kept,built)
    plan.rebuilt_at=now()
    if user:audit(db,user,action,{'plan':plan.id,'limit':plan.daily_limit,'claimed':len(kept)})
    db.commit();return plan
def set_limit(db,data,user):
    plan=_today_plan(db,True)
    plan.daily_limit=data.daily_limit
    kept,built=_build_candidates(db,plan)
    _allocate(db,plan,kept,built)
    plan.rebuilt_at=now()
    audit(db,user,'设置每日复测上限',{'plan':plan.id,'limit':plan.daily_limit})
    db.commit();return plan
def invalidate_for_observation(db,folio):
    # 新测量登记：该册未领取的旧推荐立即失效，随后按剩余名额补位。
    plan=db.scalar(select(RetestPlan).where(RetestPlan.plan_date==now().date()).with_for_update().execution_options(populate_existing=True))
    if not plan:return
    rows=db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.accession_id==folio.id,RetestCandidate.claimed==False,RetestCandidate.invalid==False)).all()
    changed=bool(rows)
    for c in rows:c.invalid=True;c.invalid_reason='该册已登记新的病害测量';c.selected=False;c.cabinet_seat=False
    if changed:
        db.flush()
        kept={c.accession_id:c for c in db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.claimed==True)).all()}
        built={c.id:c for c in db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.claimed==False,RetestCandidate.invalid==False)).all()}
        _allocate(db,plan,kept,built);plan.rebuilt_at=now();db.commit()
def invalidate_for_exit(db,folio):
    # 出柜后：该册未领取的旧推荐失效并释放名额，由仍在柜的候选补位。
    plan=db.scalar(select(RetestPlan).where(RetestPlan.plan_date==now().date()).with_for_update().execution_options(populate_existing=True))
    if not plan:return
    rows=db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.accession_id==folio.id,RetestCandidate.claimed==False,RetestCandidate.invalid==False)).all()
    if not rows:return
    for c in rows:c.invalid=True;c.invalid_reason='卷册已出柜，复测推荐失效';c.selected=False;c.cabinet_seat=False
    db.flush()
    kept={c.accession_id:c for c in db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.claimed==True)).all()}
    built={c.id:c for c in db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.claimed==False,RetestCandidate.invalid==False)).all()}
    _allocate(db,plan,kept,built);plan.rebuilt_at=now();db.commit()
def claim(db,candidate_id,user):
    # 计划行级锁串行化并发领取，保证领取数不超过当天上限；同册当天已领不重复派名。
    plan=_today_plan(db,True)
    row=get(db,RetestCandidate,candidate_id,True)
    reject(row.plan_id!=plan.id,'该推荐不属于当天名单')
    reject(row.invalid,'推荐已失效（新测量或已出柜），请领取补位名单')
    reject(row.claimed,'该名额已被领取')
    reject(not row.selected,'该册未入选当天复测名额')
    mine=db.scalar(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.accession_id==row.accession_id,RetestCandidate.claimed==True))
    reject(mine is not None,'该册今天已领取过复测名额')
    used=db.scalar(select(func.count()).select_from(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.claimed==True))
    reject(used>=plan.daily_limit,'当天复测名额已领满')
    row.claimed=True;row.claimed_by=user.username;row.claimed_at=now()
    return finish(db,user,'领取每日复测名额',row)
def snapshot(db):
    plan=_today_plan(db)
    # 请求会话退出仅 close 不 commit，首次访问懒建的计划需显式提交，否则每天重复建行。
    db.commit()
    candidates=db.scalars(select(RetestCandidate).where(RetestCandidate.plan_id==plan.id).order_by(RetestCandidate.invalid,RetestCandidate.selected.desc(),RetestCandidate.probability.desc(),RetestCandidate.cabinet_id,RetestCandidate.accession_id)).all()
    claimed=db.scalar(select(func.count()).select_from(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.claimed==True))
    selected=db.scalar(select(func.count()).select_from(RetestCandidate).where(RetestCandidate.plan_id==plan.id,RetestCandidate.selected==True,RetestCandidate.invalid==False))
    return {'plan':plan,'candidates':candidates,'claimed_count':claimed or 0,'selected_count':selected or 0}
