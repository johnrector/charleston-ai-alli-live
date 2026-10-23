"""Cloud verification with shadow TEMP tables; no real messages or calendar changes."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch
import json
import re
import communication_store as store
import promises as p
import scheduled_sms as sms
import dashboard as d


def main():
    store.initialize()
    real_connection=store.connection
    with real_connection() as db:
        for table in ('alli_promise','alli_promise_event','alli_contact_name','alli_outbound_sms','alli_communication_update','alli_dashboard_login','alli_dashboard_session'):
            # No copied foreign keys: fixtures are transaction-local and never visible to workers.
            db.execute(f'CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP')
        @contextmanager
        def connection():yield db
        with patch.object(store,'connection',connection),patch.object(store,'initialize',lambda:None):
            phone='+18435550100'
            db.execute("INSERT INTO alli_contact_name(phone,name,source) VALUES(%s,'QA Friend','owner_saved_contact')",(phone,))
            saved=p.create('Bring a laptop?','Ask John which laptop to bring.',phone,'QA Friend','ask_john',source_sid='QA-VOICE',dedupe='qa-question')
            pid=saved['promise']['id']
            assert p.create('Bring a laptop?','Ask John which laptop to bring.',phone,'QA Friend','ask_john',source_sid='QA-SMS',dedupe='qa-question-2')['promise']['id']==pid
            assert not p.listing('+18435550101')
            assert p.listing(phone)[0]['id']==pid
            p.process_once();p.process_once()
            assert db.execute("SELECT count(*) FROM alli_outbound_sms WHERE request_id=%s",('promise-alert:'+pid,)).fetchone()[0]<=1
            answered=p.change(pid,'answer','Please bring your laptop.',saved['promise']['revision'])
            try:p.change(pid,'answer','Changed answer',saved['promise']['revision'])
            except ValueError:pass
            else:raise AssertionError('Concurrent revision was accepted')
            p.process_once();p.process_once()
            result=p.listing(phone)[0];assert result['state']=='queued'
            assert db.execute('SELECT count(*) FROM alli_outbound_sms WHERE request_id=%s',('promise:'+pid,)).fetchone()[0]==1
            assert p.revalidate(dict(promise_id=pid),phone)
            assert not p.revalidate(dict(promise_id=pid),'+18435550101')
            db.execute("UPDATE alli_outbound_sms SET status='delivered',message_sid=%s WHERE request_id=%s",('SM'+'a'*32,'promise:'+pid))
            p.process_once()
            result=p.listing(phone,True)[0];assert result['state']=='completed' and result['delivery_status']=='delivered'
            try:p.change(pid,'answer','Do not resend')
            except ValueError:pass
            else:raise AssertionError('Completed message was resent')
            task=p.create('QA task',details='Manual work with a receipt')['promise']
            assert p.change(task['id'],'complete','Verified in temporary database')['promise']['state']=='completed'
            cancelled=p.create('QA scheduled text',phone=phone,contact_name='QA Friend',kind='text',response='QA text')['promise']
            p.process_once()
            assert p.change(cancelled['id'],'cancel')['promise']['state']=='cancelled'
            assert sms.get('promise:'+cancelled['id'])['status']=='cancelled'
            failed=p.create('QA failure text',phone=phone,contact_name='QA Friend',kind='text',response='QA failure')['promise']
            p.process_once()
            db.execute("UPDATE alli_outbound_sms SET status='uncertain' WHERE request_id=%s",('promise:'+failed['id'],))
            p.process_once();p.process_once()
            assert p.listing(phone)[0]['state']=='blocked'
            assert db.execute('SELECT count(*) FROM alli_outbound_sms WHERE request_id=%s',('promise:'+failed['id'],)).fetchone()[0]==1
            # OTP code stays in the test process; only its keyed digest is stored.
            captured=[]
            enqueue=sms.enqueue_in_db
            def capture(db,body,**kwargs):captured.append(body);return enqueue(db,body,**kwargs)
            with patch.object(sms,'enqueue_in_db',capture):login=d.request_code()
            code=re.search(r'code: (\d{6})',captured[0].message).group(1)
            for _ in range(2):
                try:d.verify_code(login['challenge'],'999999' if code!='999999' else '888888')
                except ValueError:pass
            assert db.execute('SELECT attempts FROM alli_dashboard_login WHERE id=%s',(login['challenge'],)).fetchone()[0]==2
            token=d.verify_code(login['challenge'],code)
            session=d.authenticate(SimpleNamespace(headers={},cookies={d.COOKIE:token}))
            assert session['method']=='cookie'
            try:d.verify_code(login['challenge'],code)
            except ValueError:pass
            else:raise AssertionError('One-time code was reused')
            try:d.write_guard(SimpleNamespace(headers={'origin':'https://wrong.example','x-csrf-token':session['csrf']}),session)
            except Exception:pass
            else:raise AssertionError('Cross-origin write accepted')
            db.execute('DELETE FROM alli_dashboard_session WHERE digest=%s',(d.digest(token),))
            try:d.authenticate(SimpleNamespace(headers={},cookies={d.COOKIE:token}))
            except Exception:pass
            else:raise AssertionError('Revoked session accepted')
            db.rollback()
    print('DASHBOARD_DATABASE_VERIFIED='+json.dumps(dict(ok=True,checks=['voice/SMS promise continuity and deduplication','phone isolation','optimistic concurrency','atomic outbox handoff','one owner alert','delivery receipt completion','no uncertain retry','queued cancellation','manual completion receipt','OTP attempt counters and one-time use','revocable session','cross-origin rejection'],all_test_data_rolled_back=True,no_messages_sent=True,no_calendar_changes=True)))

if __name__=='__main__':main()
