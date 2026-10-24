"""Legacy unit scenarios represent established sessions, not production bypasses.

Hardening tests opt out and exercise the real access boundaries explicitly.
No behavior here is imported by the application.
"""
import pytest

@pytest.fixture(autouse=True)
def isolated_boundary_services(request,monkeypatch):
    if request.node.path.name=='test_hardening.py':return
    import security_controls as security,owner_access,public_profile,contact_events,conference_notes
    for name in ('reserve','reserve_in_db','reserve_sms','model','settle_model','settle_work','finish_voice','calendar_write'):
        monkeypatch.setattr(security,name,lambda *a,**k:None)
    monkeypatch.setattr(security,'blocked',lambda p:False)
    monkeypatch.setattr(owner_access,'authorized',lambda phone,*a:security.owner(phone))
    monkeypatch.setattr(owner_access,'mark_sms',lambda *a:None)
    monkeypatch.setattr(owner_access,'end_call',lambda *a:None)
    monkeypatch.setattr(owner_access,'grant_sms',lambda *a:None)
    monkeypatch.setattr(public_profile,'read',lambda:dict(body=public_profile.DEFAULT,sources=public_profile.SOURCES))
    monkeypatch.setattr(contact_events,'bind',lambda *a:None)
    monkeypatch.setattr(conference_notes,'recover',lambda:None)
    monkeypatch.setattr(__import__('call_followthrough'),'recover',lambda:None)
