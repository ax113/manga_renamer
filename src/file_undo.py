"""Build per-comic reverse plans from durable operation references."""
import copy
import os
from pathlib import Path
from .file_tasks import object_identity, identity_key, same_identity
from .file_operations import result_status
from .session_store import item_to_dict


def undo_plans(store, original, items, session_id, include_moves=False):
    related = [t for t in store.list() if t.get('parent_task_id') == original['task_id']
        and t['library_id'] == original['library_id'] and t['action'] in {'undo','undo_move'}]
    if any(any(r['state'] != 'success' for r in t['rows']) for t in related):
        raise ValueError('已有未完成的撤销任务，请选择该任务继续或重试')
    # A composite undo may already own a move row, though its main parent is rename.
    for task in store.list():
        if task['library_id'] != original['library_id']: continue
        if any(r['state'] != 'success' and any(s.get('reverses_task_id') == original['task_id'] for s in r.get('steps',[])) for r in task['rows']):
            raise ValueError('关联撤销任务尚未完成，请先去该任务继续或重试')
    undone = store.undone_rows(original['task_id'])
    receipts = store.receipts(original['library_id'])
    plans = []
    for old in original['rows']:
        if old['state'] != 'success' or old['local_id'] in undone: continue
        receipt = receipts.get(identity_key(old.get('tracking_identity',old['identity'])), {})
        identity=receipt.get('identity',old.get('result_identity',old['identity']))
        operations = receipt.get('operations', [])
        pos = next((n for n, op in enumerate(operations) if (op['task_id'],op['row_id']) == (original['task_id'],old['local_id'])), None)
        source = receipt.get('path', old['target_path'])
        matches = [i for i in items if i.original_path == source and same_identity(i.original_path,identity)]
        item = matches[0] if len(matches) == 1 else None
        problem = ''
        tail = operations[pos+1:] if pos is not None else []
        if pos is None: problem = '无法关联原操作记录，请重新核对'
        elif any(op['action'] == original['action'] for op in tail):
            problem = ('有后续改名，请先恢复最近一次改名' if original['action']=='rename'
                       else '有后续移动，请先移回最近一次移动')
        elif not item and original['action']=='rename' and not tail:
            problem = '记录对象不在当前扫描范围内，无法安全撤销'
        # Names and locations are independent dimensions. Historical paths are
        # evidence for each dimension, not a requirement to visit them again.
        # A combined rename reversal additionally reverses later move receipts.
        reverse = ([op for op in reversed(operations[pos+1:]) if op['action']=='move']
                   if include_moves and original['action']=='rename' and pos is not None else [])
        if pos is not None:
            reverse.append(operations[pos])
        steps = []
        remaining = list(operations)
        current = source
        for op in reverse:
            operation = 'undo_move' if op['action']=='move' else 'undo'
            remaining = [x for x in remaining if (x['task_id'],x['row_id']) != (op['task_id'],op['row_id'])]
            labels = {'已改名' if x['action']=='rename' else '已移动' for x in remaining} | set(receipt.get('retained_labels', []))
            target = (os.path.join(os.path.dirname(op['source_path']), Path(current).name)
                      if operation=='undo_move' else
                      os.path.join(os.path.dirname(current), Path(op['source_path']).name))
            step = {'source_path':current,'target_path':target,
                'operation':operation,'label':'移回原处' if operation=='undo_move' else '恢复原名',
                'reverses_task_id':op['task_id'],'reverses_row':op['row_id'],
                'result_status':result_status(labels,operation),'state':'pending','reason':''}
            if operation=='undo_move':
                try: step['target_parent_identity']=object_identity(os.path.dirname(step['target_path']))
                except (OSError,ValueError) as exc: problem=problem or str(exc)
            steps.append(step)
            current = target
        operation = 'undo_move' if original['action']=='move' else 'undo'
        plan = copy.deepcopy(old)
        plan.pop('steps',None)
        plan.pop('transfer',None)
        plan.pop('result_identity',None)
        plan['identity']=copy.deepcopy(identity)
        plan.update(source_path=source, target_path=current, current_name=Path(source).name,
            final_name=Path(current).name, parent_row=old['local_id'], previous_status=receipt.get('status','已改名'),
            operation=operation, session_id=session_id, local_id=item.local_id if item else old['local_id'],
            item_snapshot=item_to_dict(item) if item else receipt.get('snapshot',old.get('item_snapshot',{})),
            plan_problem=problem, needs_move=any(op['action']=='move' for op in tail) and original['action']=='rename',
            include_moves=bool(include_moves and original['action']=='rename'))
        if len(steps) > 1:
            plan['steps']=steps
        elif steps:
            plan.update({k:v for k,v in steps[0].items() if k not in {'state','reason'}})
        plans.append(plan)
    return plans
