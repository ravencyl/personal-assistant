"""活动依赖关系图数据 API"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from core.utils import visible_qs
from ..models import Activity


@login_required
def dependency_data(request):
    """返回活动依赖关系 JSON（节点 + 边），供前端力导向图渲染。

    GET /activities/dependency-data/[?activity=<id>]

    - 不带 activity：全量依赖网络（原列表页行为，保留兼容）。
    - 带 activity：只返回与该活动连通的依赖子图——沿 blocked_by 边双向
      BFS（前置的前置、后续的后续都算），详情页的图只展示本活动所在的
      网络（2026-10-08 图从列表页搬到详情页，它是绑定活动的）。
      越权/不存在的 id 返回空图而不是 404：消费方是 fetch 组件，只认 JSON。

    返回: {nodes: [{id, name, status}], edges: [{from, to}]}
    """
    activities = list(visible_qs(Activity, request.user).prefetch_related('blocked_by'))

    anchor = request.GET.get('activity')
    if anchor:
        try:
            anchor_id = int(anchor)
        except (TypeError, ValueError):
            return JsonResponse({'nodes': [], 'edges': []})
        by_id = {a.id: a for a in activities}
        if anchor_id not in by_id:
            return JsonResponse({'nodes': [], 'edges': []})

        # child -> 前置 id 集合；再反查一次得到 child -> 后续 id 集合
        parents = {a.id: {d.id for d in a.blocked_by.all()} for a in activities}
        children = {}
        for child_id, ps in parents.items():
            for pid in ps:
                children.setdefault(pid, set()).add(child_id)

        keep = set()
        frontier = {anchor_id}
        while frontier:
            keep |= frontier
            nxt = set()
            for aid in frontier:
                nxt |= parents.get(aid, set())
                nxt |= children.get(aid, set())
            frontier = nxt - keep
        activities = [by_id[i] for i in keep if i in by_id]

    # 只返回参与了依赖关系的活动作为节点：没配置依赖时图区是干净的空状态，
    # 而不是一整圈无连线的孤立圆点
    nodes_by_id = {}
    edges = []
    for a in activities:
        for dep in a.blocked_by.all():
            edges.append({'from': dep.id, 'to': a.id})
            nodes_by_id[dep.id] = {'id': dep.id, 'name': dep.name, 'status': dep.status}
            nodes_by_id[a.id] = {'id': a.id, 'name': a.name, 'status': a.status}

    return JsonResponse({'nodes': list(nodes_by_id.values()), 'edges': edges})
