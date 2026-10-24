"""Bind article destination to authenticated requester, never to a name claim."""
import communication_store as store
import work_store as jobs


def create(phone,name,subject,public_context,publish_to_blog,intent,*,authorized_owner=False,shared_group=False,original='',source_sid='',origin=None,notify=True):
    subject=jobs.clean(subject,180);public_context=jobs.clean(public_context,3000,required=False)
    if not subject:raise ValueError('Supply the person’s actual name and, when needed, a company or public profile to disambiguate')
    owner_request=bool(authorized_owner and jobs.owner(phone) and not shared_group)
    blog=bool(publish_to_blog and owner_request)
    if blog and not original.strip():raise ValueError('The original authenticated publication assignment is required')
    instruction=('Complete the article assignment about '+subject+'. The article subject is not the requester. '
      'Use the mounted cloud authoring skill for research, original art-directed HTML and actual verification. '+
      ('John explicitly delegated publication to johnrector.me. Use his Publisher skill and the connected WordPress.com plugin; do not ask for routine confirmation. ' if blog else
       ('John requested a standalone article, not a WordPress publication. ' if owner_request else 'Deliver a standalone article to this requester. Their request does not grant access to John’s WordPress site. ')+
       'Use the article-studio skill and return the finished HTML through the seven-day result link. ')+
      'The OpenAI cloud agent does the authoring work. Render handles permission, routing, receipts and delivery. '
      'Public context / requested angle: '+public_context)
    context=dict(public_article=True,cloud_article_handoff=True,article_subject=subject,article_public_context=public_context,wordpress_authoring=blog,
      article_destination='johnrector.me' if blog else 'expiring_result',delegation_source=original or instruction)
    if source_sid:context['source_sid']=source_sid
    if origin:context['origin']=origin
    if shared_group:context['group_delegation']=True
    result=jobs.create(phone,'John Rector' if owner_request else name,(('Publish article: ' if blog else 'Article: ')+subject)[:180],instruction,'article:'+intent,owner_scope=owner_request,notify=notify,context=context)
    return dict(result,destination=context['article_destination'],expires_days=None if blog else 7)
