"""活动依赖关系图数据 API"""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from core.utils import visible_qs
from ..models import Activity


@login_required
def dependency_data(request):
    """返回活动依赖关系 JSON（节点 + 边），供前端力导向图渲染。

    GET /activities/dependency-data/
    返回: {nodes: [{id, name, status}], edges: [{from, to}]}
    """
    activities = visible_qs(Activity, request.user).prefetch_related('blocked_by')

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
