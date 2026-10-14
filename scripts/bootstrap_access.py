"""Create an administrator and initial invitation without printing secrets.

Run with the project's Python: python -s -m scripts.bootstrap_access --help.
The private credentials file must stay outside any served directory.
"""
from __future__ import annotations
import argparse
import csv
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from uuid import uuid4

import asyncpg
from backend.api.v1.auth import pwd_context, RegisterRequest
from backend.config import get_settings


async def bootstrap(args):
    password = secrets.token_urlsafe(24)
    request = RegisterRequest(username=args.username, email=args.email, password=password)
    destination = Path(args.credentials_file).resolve()
    if destination.exists():
        raise SystemExit('Credentials destination already exists; choose a new private file')
    settings = get_settings()
    connection = await asyncpg.connect(host=settings.db_host, port=settings.db_port,
        database=settings.db_name, user=settings.db_user, password=settings.db_password)
    try:
        async with connection.transaction():
            await connection.execute('SELECT pg_advisory_xact_lock(710120261012)')
            exists = await connection.fetchval('''SELECT id FROM users
                WHERE lower(username) IN ($1,$2) OR lower(email) IN ($1,$2)''',
                request.username.lower(), str(request.email).lower())
            if exists:
                raise SystemExit('Administrator username/email already exists; no account changed')
            user_id = uuid4()
            await connection.execute('''INSERT INTO users
                (id,tenant_id,username,email,password_hash,role)
                VALUES ($1,$2,$3,$4,$5,'admin')''', user_id, settings.default_tenant_id,
                request.username, str(request.email).lower(), await asyncio.to_thread(pwd_context.hash,password))
            invitation = secrets.token_urlsafe(24)
            expires = datetime.now(timezone.utc) + timedelta(days=7)
            await connection.execute('''INSERT INTO registration_invites
                (id,token_hash,token_hint,tenant_id,created_by,label,max_uses,expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)''', uuid4(),
                hashlib.sha256(invitation.encode()).hexdigest(), invitation[-6:],
                settings.default_tenant_id, user_id, '第一版公司试用', args.invite_uses, expires)
            disabled=[]
            if args.disable_demo_users:
                known={'admin':'123456','Lee01':'Lee@123456','Sra01':'Sra@123456','Sra02':'Sra@123456'}
                for username, old_password in known.items():
                    for row in await connection.fetch('''SELECT id,password_hash FROM users
                        WHERE tenant_id=$1 AND username=$2 AND is_active=TRUE''', settings.default_tenant_id, username):
                        if await asyncio.to_thread(pwd_context.verify,old_password,row['password_hash']):
                            await connection.execute('UPDATE users SET is_active=FALSE WHERE id=$1',row['id'])
                            disabled.append(str(row['id']))
            # Write before commit: an inaccessible destination must not create an
            # administrator whose generated password cannot be retrieved.
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = {'server': args.server_url, 'admin_username': request.username,
                    'admin_password': password, 'registration_url': args.server_url.rstrip('/')+'/api/v1/auth/ui',
                    'invite_code': invitation, 'invite_expires_at': expires.isoformat(),
                    'invite_max_uses': args.invite_uses}
            descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
            if os.name=='nt':
                result=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,check=True)
                identity='*'+next(csv.reader(result.stdout.decode(errors='replace').splitlines()))[1]
                acl=subprocess.run(['icacls',str(destination),'/inheritance:r','/grant:r',identity+':F','*S-1-5-32-544:F'],capture_output=True)
                if acl.returncode:raise RuntimeError('Unable to restrict credentials file access')
        print(json.dumps({'created': True, 'admin_username': request.username,
                          'credentials_file': str(destination), 'disabled_demo_accounts':len(disabled)}, ensure_ascii=False))
    finally:
        await connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--username', default='company-admin')
    parser.add_argument('--email', required=True)
    parser.add_argument('--server-url', required=True)
    parser.add_argument('--credentials-file', required=True)
    parser.add_argument('--invite-uses', type=int, default=20, choices=range(1,101))
    parser.add_argument('--disable-demo-users', action='store_true', help='Disable only known seeded accounts still using their original demo passwords')
    args = parser.parse_args()
    if __import__('sys').platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(bootstrap(args))


if __name__ == '__main__':
    main()
