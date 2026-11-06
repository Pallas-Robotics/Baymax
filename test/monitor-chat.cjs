const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{
 constructor(){this.children=[];this.textContent='';this.scrollHeight=0;this.clientHeight=420;this.scrollTop=0;}
 get firstChild(){return this.children[0]||null;}
 get nextSibling(){if(!this.parent)return null;return this.parent.children[this.parent.children.indexOf(this)+1]||null;}
 append(...nodes){for(const n of nodes){n.remove();n.parent=this;this.children.push(n);}}
 replaceChildren(...nodes){for(const n of this.children)n.parent=null;this.children=[];this.append(...nodes);}
 remove(){if(this.parent){const at=this.parent.children.indexOf(this);this.parent.children.splice(at,1);this.parent=null;}}
 insertBefore(n,ref){n.remove();n.parent=this;const at=ref?this.children.indexOf(ref):this.children.length;this.children.splice(at,0,n);}
 querySelector(selector){return this.children.find(x=>'#'+x.id===selector);}
}
const conversation=new Element();
const context=vm.createContext({document:{getElementById:id=>{assert.equal(id,'conversation');return conversation;},createElement:()=>new Element()}});
const html=fs.readFileSync(require('node:path').join(__dirname,'../Baymax-main/static/emberdevUI.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0].split('async function update(){')[0];
vm.runInContext(script+';globalThis.chat={renderConversation,speechRows,activityRows};',context);
const turn={id:1,user_id:7,speech_start:1000,speech_end:1000.8,transcript_first:1001,transcript_last:1001.2,association:'Local speech estimate',delays:{recognition_ms:200}};
const start={id:'tool-1',state:'toolkit',name:'get_weather',time:1002,started_at:1002,finished_at:null};
const data={pid:1,heartbeat:1005,transcripts:[{id:7,speaker:'user',time:1001,text:'What is the weather?',live:true}],events:[start],conversation_timing:[turn]};
context.chat.renderConversation(data);
assert.equal(conversation.children.length,2);
const user=conversation.children[0],tool=conversation.children[1];
assert.match(user.children[2].textContent,/Local speech/);
assert.match(user.children[2].textContent,/Speech end → transcript 0.200 s/);
assert.match(user.children[2].textContent,/Waiting for reply/);
assert.match(tool.children[1].textContent,/get_weather · running/);
const finish={...start,time:1003.5,finished_at:1003.5};
const complete={...turn,reply_text:1003,reply_audio:1003.6,playback:1004,reply_complete:1004.1,delays:{recognition_ms:200,transcript_to_reply_ms:2600,playback_wait_ms:400,audible_ms:3200}};
const updated={...data,events:[start,finish,{state:'thinking',time:1003.5,message:'Toolkit finished; waiting for Gemini reply'}],transcripts:[...data.transcripts,{id:8,speaker:'ember',time:1003,text:'Sunny.'}],conversation_timing:[complete]};
context.chat.renderConversation(updated);
assert.equal(conversation.children.length,3);
assert.equal(conversation.children[0],user);
assert.equal(conversation.children[1],tool);
assert.match(tool.children[1].textContent,/finished · 1.500 s/);
assert.match(tool.children[2].textContent,/Started .* · Finished /);
assert.doesNotMatch(user.children[2].textContent,/Waiting for reply/);
assert.match(conversation.children[2].children[2].textContent,/speech end → playback 3.200 s/);
assert.match(conversation.children[2].children[2].textContent,/audio → playback 0.400 s/);
const repeated={...updated,events:[finish,{...start,id:'tool-2',time:1006,started_at:1006}]};
context.chat.renderConversation(repeated);
assert.equal(conversation.children.filter(x=>x.className==='message activity').length,2);
assert.equal(conversation.children[1],tool);
console.log('PASS: inline latency updates, stable single toolkit bubble, deduplication and distinct repeated calls');
