const state = {busy:false};
const feedback=document.getElementById('feedback');
async function request(path,payload){
 state.busy=true;feedback.hidden=false;feedback.textContent='正在处理…';
 try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok)throw new Error(data.detail||'运行失败，请重试');location.reload();}
 catch(error){feedback.textContent=error.message;}finally{state.busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
document.getElementById('run').addEventListener('click',()=>{
 if(state.busy)return;
 document.getElementById('run').disabled=true;
 const raw=document.getElementById('csv-text')?.value;
 request('/api/runs',{sample:document.getElementById('sample').value,...(raw?{csv_text:raw}:{}),threshold:document.getElementById('threshold')?.value||'8',max_gap:Number(document.getElementById('gap')?.value||15)});
});
document.getElementById('review')?.addEventListener('click',event=>{if(state.busy)return;const b=event.currentTarget;b.disabled=true;request('/api/batches/review',{run_id:b.dataset.run,version:Number(b.dataset.version)});});
function closingRequest(path,payload){
 const day=payload.day;
 document.querySelectorAll('.closing-grid button,.versions button').forEach(b=>b.disabled=true);
 request(path,payload);
}
document.getElementById('publish')?.addEventListener('click',event=>{
 const b=event.currentTarget;closingRequest('/api/closing/publish',{day:b.dataset.day,policy:b.dataset.policy});
});
document.getElementById('restore')?.addEventListener('click',event=>{
 const b=event.currentTarget;if(!b.dataset.version)return;
 closingRequest('/api/closing/restore',{day:b.dataset.day,version:Number(b.dataset.version)});
});
document.querySelectorAll('.confirm').forEach(button=>button.addEventListener('click',event=>{
 const b=event.currentTarget;closingRequest('/api/closing/confirm',{day:b.dataset.day,version:Number(b.dataset.version),note:b.dataset.note||''});
}));
document.querySelectorAll('.restore-version').forEach(button=>button.addEventListener('click',event=>{
 const b=event.currentTarget;closingRequest('/api/closing/restore',{day:b.dataset.day,version:Number(b.dataset.version)});
}));
