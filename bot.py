"""Telegram long-polling worker. Run exactly one instance alongside the Flask web process."""
import os, time, logging, urllib.request, urllib.parse, json, threading
from concurrent.futures import ThreadPoolExecutor
from main import BOT_TOKEN, process_update, tg
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s bot: %(message)s')
log=logging.getLogger('jinny-bot')
# Different users are processed concurrently while preserving update order per user.
from collections import deque
_user_queues={}
_active_users=set()
_user_queue_lock=threading.Lock()

def _safe_process(item):
    try: process_update(item)
    except Exception: log.exception('Update %s failed',item.get('update_id'))

def _drain_user(uid, slots):
    while True:
        with _user_queue_lock:
            queue=_user_queues.get(uid)
            if not queue:
                _user_queues.pop(uid,None)
                _active_users.discard(uid)
                return
            item=queue.popleft()
        _safe_process(item)
        slots.release()

def enqueue_update(item, pool, slots):
    slots.acquire()  # Back-pressure: never grow an unbounded update queue.
    message=item.get('message') or {}
    callback=item.get('callback_query') or {}
    user=(message.get('from') or callback.get('from') or {})
    uid=user.get('id')
    if uid is None:
        future=pool.submit(_safe_process,item)
        future.add_done_callback(lambda _future: slots.release())
        return
    uid=int(uid)
    with _user_queue_lock:
        queue=_user_queues.setdefault(uid,deque())
        queue.append(item)
        if uid in _active_users:
            return
        _active_users.add(uid)
    pool.submit(_drain_user,uid,slots)


def request(method, params=None, timeout=40):
    data=urllib.parse.urlencode(params or {}).encode()
    req=urllib.request.Request(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',data=data)
    with urllib.request.urlopen(req,timeout=timeout) as r: result=json.loads(r.read().decode())
    if not result.get('ok'): raise RuntimeError(result.get('description','Telegram API error'))
    return result['result']

def main():
    if not BOT_TOKEN: raise SystemExit('BOT_TOKEN chưa được cấu hình')
    # Ensure stale webhook is removed before polling. drop_pending_updates=False preserves queued updates.
    request('deleteWebhook',{'drop_pending_updates':'false'})
    offset=None
    pool=ThreadPoolExecutor(max_workers=6,thread_name_prefix='tg-update')
    pending_slots=threading.BoundedSemaphore(128)
    log.info('Polling started; webhook removed. Update workers: 6')
    try:
        while True:
            try:
                updates=request('getUpdates',{'timeout':30,'allowed_updates':json.dumps(['message','callback_query']),**({'offset':offset} if offset is not None else {})},timeout=40)
                for item in updates:
                    offset=item['update_id']+1
                    enqueue_update(item,pool,pending_slots)
            except KeyboardInterrupt: break
            except Exception: log.exception('Polling error; retry in 5 seconds'); time.sleep(5)
    finally:
        pool.shutdown(wait=True,cancel_futures=False)


if __name__=='__main__': main()
