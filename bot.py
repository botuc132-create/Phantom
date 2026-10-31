"""Telegram long-polling worker. Run exactly one instance alongside the Flask web process."""
import os, time, logging, urllib.request, urllib.parse, json
from main import BOT_TOKEN, process_update, tg
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s bot: %(message)s')
log=logging.getLogger('jinny-bot')

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
    log.info('Polling started; webhook removed.')
    while True:
        try:
            updates=request('getUpdates',{'timeout':30,'allowed_updates':json.dumps(['message','callback_query']),**({'offset':offset} if offset is not None else {})},timeout=40)
            for item in updates:
                offset=item['update_id']+1
                try: process_update(item)
                except Exception: log.exception('Update %s failed',item.get('update_id'))
        except KeyboardInterrupt: break
        except Exception: log.exception('Polling error; retry in 5 seconds'); time.sleep(5)

if __name__=='__main__': main()
