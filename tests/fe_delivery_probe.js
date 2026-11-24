const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('pp6kit/static/app.js','utf8');
const state={}; let parsed,mode;
const ctx={window:{},state,JSON,Set,Object,fetch:async(u,o)=>{parsed=o.headers;return {ok:true,json:async()=>({ok:true,modules:[{code:'bo',count:1}]})};},switchMode:m=>{mode=m;state.flowMode=m==='workflow';},renderAll:()=>{state.picked=new Set(['wrong']);},renderModules:()=>{},updateBar:()=>{},goStep:()=>{},toast:()=>{}};
ctx.Relations={restore:(scope,d)=>ctx.decisions=d};ctx.window.Relations=ctx.Relations;
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('window.PP6TaskBridge =')),ctx);
(async()=>{
const headers={'X-Modules':'["form"]','X-Workflow-All':'1','X-Object-Scope':'all','X-Project-Link':JSON.stringify({version:1,objects:{test:{linked:false,align:{x:'target'}}}}),'X-Relations':JSON.stringify({version:1,decisions:{test:{enabled:false}}})};
await ctx.window.PP6TaskBridge.restore({params:headers},{arrayBuffer:async()=>new ArrayBuffer(0)});
assert.deepStrictEqual(parsed,headers);assert.equal(mode,'workflow');assert.equal(state.restoredObjectScope,'all');assert.deepStrictEqual([...state.picked],['form']);assert.equal(state.plLinked.test,false);assert.equal(ctx.decisions.test.enabled,false);
const start=source.indexOf('function projLinkHeader()');const end=source.indexOf('/* AI 生成/导出这条链',start);vm.runInContext(source.slice(start,end),ctx);
assert.equal(JSON.parse(ctx.projLinkHeader()).objects.test.linked,false);
state.plLinked.test=true;assert.equal(JSON.parse(ctx.projLinkHeader()).objects.test.linked,true);
const relations=fs.readFileSync('pp6kit/static/relations.js','utf8');const rc={window:{},document:{querySelectorAll:()=>[]}};vm.createContext(rc);vm.runInContext(relations,rc);rc.window.Relations.restore('parse',{test:{enabled:false}});assert.equal(JSON.parse(rc.window.Relations.decisionsHeader('parse')).decisions.test.enabled,false);
console.log('恢复模块/流程/scope/项目关联、修改生效、关系显式取消全部通过');
})().catch(e=>{console.error(e);process.exitCode=1});
