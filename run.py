"""Render entrypoint: supervise Gunicorn website and the single Telegram polling worker."""
import os, sys, signal, subprocess, time

def main():
    env=os.environ.copy(); port=env.get('PORT','10000')
    web=subprocess.Popen([sys.executable,'-m','gunicorn','--workers','2','--threads','4','--bind',f'0.0.0.0:{port}','--timeout','120','main:app'],env=env)
    bot=subprocess.Popen([sys.executable,'bot.py'],env=env) if env.get('BOT_TOKEN','').strip() else None
    children=[p for p in (web,bot) if p]
    def stop(*_):
        for p in children:
            if p.poll() is None: p.terminate()
        for p in children:
            try: p.wait(timeout=10)
            except subprocess.TimeoutExpired: p.kill()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    while True:
        if web.poll() is not None:
            code=web.returncode
            if bot and bot.poll() is None: bot.terminate()
            raise SystemExit(code or 1)
        if bot and bot.poll() is not None:
            # Restart failed bot worker without touching the web service.
            time.sleep(2); bot=subprocess.Popen([sys.executable,'bot.py'],env=env); children[1]=bot
        time.sleep(2)

if __name__=='__main__': main()
