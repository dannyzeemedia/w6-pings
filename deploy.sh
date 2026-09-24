#!/bin/bash
# Render has no webhook on this repo (its GitHub app isn't installed on it), so pushes don't auto-deploy.
# Run after `git push`: triggers a deploy of the latest commit and waits for it to go live.
python3 - <<'PY'
import json,requests,time,warnings;warnings.filterwarnings('ignore')
from dotenv import dotenv_values
import gspread
from google.oauth2 import service_account
e=dotenv_values('/home/danny/zm-ai-agent/.env')
gc=gspread.authorize(service_account.Credentials.from_service_account_info(json.loads(e['GOOGLE_SERVICE_ACCOUNT_JSON']),scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']))
RK=next(r[3] for r in gc.open_by_key('1VyrxCs_NI-Lcc_eY_i0jsfcfcWU8ujkGx-dvEpYE_XQ').sheet1.get_all_values() if r[2]=='claude-zm-ai-agent')
H={"Authorization":f"Bearer {RK}"};S="srv-daq7gjk9v7es73c474og"
d=requests.post(f"https://api.render.com/v1/services/{S}/deploys",headers=H,json={"clearCache":"do_not_clear"}).json()
print("deploy",d["id"],d.get("commit",{}).get("id","")[:7])
for _ in range(60):
    st=requests.get(f"https://api.render.com/v1/services/{S}/deploys/{d['id']}",headers=H).json()["status"]
    if st in("live","build_failed","update_failed","canceled","deactivated"): break
    time.sleep(10)
print("status",st)
PY
