const fs=require('fs'),path=require('path'),crypto=require('crypto');
const BASE='https://api.upload-post.com';
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
function createPublisher(root,{fetcher=fetch,autoStart=true,serviceKey=process.env.CUTROOM_PUBLISHING_KEY||'',profileName=process.env.CUTROOM_PUBLISHING_PROFILE||'cutroom-local'}={}){
 const dir=path.join(root,'publishing-data');fs.mkdirSync(dir,{recursive:true});const file=path.join(dir,'queue.json');
 let rows=[];try{rows=JSON.parse(fs.readFileSync(file,'utf8'))}catch{}
 let key=serviceKey.trim(),profiles=[],settings=null,enabled=false,busy=false;
 for(const row of rows)if(row.state==='uploading')row.state='checking';
 function save(){const tmp=file+'.tmp';fs.writeFileSync(tmp,JSON.stringify(rows,null,2));fs.renameSync(tmp,file)}
 function status(){return {signInReady:!!serviceKey.trim(),connected:!!key&&profiles.some(p=>p.accounts.tiktok||p.accounts.youtube),profiles,enabled,settings,items:rows.slice(-100).reverse().map(({file,...r})=>r)}}
 async function refreshAccounts(){if(!key)return status();profiles=await getProfiles();if(serviceKey)profiles=profiles.filter(p=>p.username===profileName);return status()}
 async function signIn(platform){
  if(!['tiktok','youtube'].includes(platform))throw Error('Choose TikTok or YouTube.');
  if(!serviceKey.trim())throw Error('Account sign-in is not configured yet. The studio administrator must configure the publishing service on the server. No API key is needed in this page.');
  if(busy)throw Error('Wait for the current upload before reconnecting accounts.');
  enabled=false;
  const existing=await getProfiles();
  if(!existing.some(p=>p.username===profileName))await api('/api/uploadposts/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:profileName})});
  const data=await api('/api/uploadposts/users/generate-jwt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:profileName,platforms:[platform],show_calendar:false,connect_title:'Connect your account to Cutroom',connect_description:'Authorize your account, then return to Cutroom to enable automatic posting.'})});
  const link=new URL(data.access_url);if(link.protocol!=='https:'||!(link.hostname==='upload-post.com'||link.hostname.endsWith('.upload-post.com'))||link.username||link.password)throw Error('Publishing service returned an invalid sign-in link.');
  return {url:link.href};
 }
 async function api(route,opts={}){const response=await fetcher(BASE+route,{...opts,headers:{Authorization:'Apikey '+key,...opts.headers},signal:AbortSignal.timeout(opts.method==='POST'?600000:30000)});let data;try{data=await response.json()}catch{throw Error('Publishing service returned an unreadable response.')}if(!response.ok)throw Error(String(data.message||data.error||'Publishing service request failed ('+response.status+').').slice(0,400));return data}
 async function getProfiles(){const data=await api('/api/uploadposts/users');if(!Array.isArray(data.profiles))throw Error('The service did not return any profiles.');return data.profiles.map(p=>({username:p.username,accounts:Object.fromEntries(['tiktok','youtube'].map(platform=>{const a=p.social_accounts?.[platform];return [platform,a&&typeof a==='object'&&a.username&&!a.reauth_required?{id:a.username,name:a.handle||a.display_name||a.username,capabilities:a.capabilities||[]}:null]}))}))}
 async function connect(input){if(busy)throw Error('Wait for the current publishing request before changing accounts.');enabled=false;key=String(input.apiKey||'').trim();if(!key)throw Error('Enter your Upload-Post API key.');try{profiles=await getProfiles();settings=null;return status()}catch(e){key='';profiles=[];throw e}}
 function configure(input){if(!key)throw Error('Connect your publishing account first.');const p=profiles.find(p=>p.username===input.profile);if(!p?.accounts.tiktok||!p.accounts.youtube)throw Error('Connect both TikTok and YouTube to the same Upload-Post profile first.');if(!p.accounts.tiktok.capabilities.includes('video_privacy'))throw Error('Reconnect TikTok in Upload-Post to enable explicit video privacy settings.');if(!['public','private'].includes(input.visibility))throw Error('Choose who can see your posts.');if(typeof input.madeForKids!=='boolean')throw Error('Choose the YouTube audience setting.');if(input.consent!==true)throw Error('Confirm automatic publishing to the displayed accounts.');settings={profile:p.username,accounts:p.accounts,visibility:input.visibility,madeForKids:input.madeForKids,caption:String(input.caption||'{title}').slice(0,1500),synthetic:!!input.synthetic,ownBrand:!!input.ownBrand,paidBrand:!!input.paidBrand};if(settings.paidBrand&&settings.visibility==='private')throw Error('Branded TikTok content needs public visibility.');enabled=true;return status()}
 function capture(){return enabled&&key&&settings?JSON.parse(JSON.stringify(settings)):null}
 function enqueue(jobId,part,clip,plan){if(!plan)return;const relative=part?path.join(jobId,'part-'+String(part).padStart(3,'0'),'clip.mp4'):path.join(jobId,'clip.mp4');const media=path.join(root,'.runtime/jobs',relative);if(!fs.existsSync(media))throw Error('Rendered clip is missing.');
  const title=String(clip.title||'My clip').slice(0,100),caption=plan.caption.replaceAll('{title}',title).replaceAll('{part}',String(part||1));
  for(const platform of ['tiktok','youtube']){const identity=JSON.stringify([clip.projectId,clip.start,clip.end,clip.style,platform,plan.accounts[platform].id,caption,plan.visibility,plan.madeForKids,plan.synthetic,plan.ownBrand,plan.paidBrand]);const id=hash(identity);if(rows.some(r=>r.id===id))continue;
   const duration=clip.end-clip.start;const reason=duration<3?'Automatic posts need clips at least 3 seconds long.':platform==='youtube'&&duration>180?'This clip exceeds the 3-minute Shorts limit.':platform==='tiktok'&&duration>600?'This clip exceeds the publishing service’s 10-minute TikTok limit.':null;
   rows.push({id,requestId:crypto.randomUUID(),jobId,part:part||0,file:relative,platform,plan,title,caption,state:reason?'blocked':'queued',message:reason||'Waiting to publish',createdAt:Date.now()});
  }save();
 }
 function applyResult(row,data){const results=Array.isArray(data.results)?data.results:Object.entries(data.results||{}).map(([platform,r])=>({platform,...r}));const r=results.find(r=>r.platform===row.platform)||{};
  if(data.status==='failed'||r.success===false){row.state='failed';row.message=String(r.error||r.message||data.message||'Platform rejected the post').slice(0,400)}
  else if(data.status==='completed'||data.status==='complete'){
   const link=r.post_url||r.url;row.url=typeof link==='string'&&/^https:\/\//.test(link)?link:null;
   row.state=r.fallback_to_inbox?'needs_action':r.success===true&&row.url?'published':'needs_action';row.message=row.state==='published'?'Published':r.fallback_to_inbox?'Delivered to TikTok inbox. Finish posting in TikTok.':'Service finished. Check the provider dashboard for the final post.';
  }else if(data.status==='not_found'){row.state='needs_action';row.message='Upload status is unknown. Check the provider before retrying.'}
  else{row.state='checking';row.message='Platform is processing your video';}
  row.updatedAt=Date.now();save();
 }
 async function tick(){if(busy||!key||!enabled)return;busy=true;try{
   const row=rows.find(r=>r.state==='checking'&&Date.now()-(r.checkedAt||0)>15000)||rows.find(r=>r.state==='queued');if(!row)return;
   if(row.state==='checking'){row.checkedAt=Date.now();try{applyResult(row,await api('/api/uploadposts/status?request_id='+encodeURIComponent(row.requestId)))}catch(e){row.message='Status check failed; will check again. '+e.message;save()}return}
   try{
    const fresh=(await getProfiles()).find(p=>p.username===row.plan.profile);if(fresh?.accounts[row.platform]?.id!==row.plan.accounts[row.platform].id)throw Error('The destination account changed or disconnected. Reconnect before posting.');
    if(!enabled||row.state!=='queued')return;
    const form=new FormData();const fields={user:row.plan.profile,title:row.title,async_upload:'true',request_id:row.requestId,external_id:row.id};
    if(row.platform==='youtube')Object.assign(fields,{youtube_title:row.title,youtube_description:row.caption,privacyStatus:row.plan.visibility,selfDeclaredMadeForKids:String(row.plan.madeForKids),containsSyntheticMedia:String(row.plan.synthetic),hasPaidProductPlacement:String(row.plan.paidBrand)});
    else Object.assign(fields,{tiktok_title:row.caption,privacy_level:row.plan.visibility==='public'?'PUBLIC_TO_EVERYONE':'SELF_ONLY',post_mode:'DIRECT_POST',disable_inbox_fallback:'true',disable_comment:'true',disable_duet:'true',disable_stitch:'true',is_aigc:String(row.plan.synthetic),brand_organic_toggle:String(row.plan.ownBrand),brand_content_toggle:String(row.plan.paidBrand)});
    for(const [k,v] of Object.entries(fields))form.append(k,v);form.append('platform[]',row.platform);form.append('video',await fs.openAsBlob(path.join(root,'.runtime/jobs',row.file),{type:'video/mp4'}),'clip.mp4');
    row.state='uploading';row.message='Uploading to '+row.platform;save();
    const data=await api('/api/upload',{method:'POST',headers:{'Idempotency-Key':row.id},body:form});if(data.request_id)row.requestId=data.request_id;applyResult(row,data);
   }catch(e){row.state=row.state==='uploading'?'checking':'failed';row.message=e.message;row.checkedAt=Date.now();save()}
  }finally{busy=false}}
 function pause(){enabled=false;return status()}
 function cancel(id){const row=rows.find(r=>r.id===id);if(!row||!['queued','failed','blocked'].includes(row.state))throw Error('Only unsent posts can be cancelled.');row.state='cancelled';row.message='Cancelled before upload';save();return status()}
 function retry(id){const row=rows.find(r=>r.id===id);if(!row||!['failed','needs_action'].includes(row.state))throw Error('This item cannot be retried.');row.state='queued';row.message='Queued with the same duplicate-prevention key';save();return status()}
 const timer=autoStart?setInterval(()=>tick().catch(()=>{}),5000):null;timer?.unref();
 return {status,connect,signIn,refreshAccounts,configure,capture,enqueue,tick,pause,cancel,retry,close:()=>clearInterval(timer)};
}
module.exports={createPublisher};
