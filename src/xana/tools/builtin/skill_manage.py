"""Explicit skill inventory, installation and per-agent registration through the normal tool chain."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import tempfile

from xana.agents.interfaces import Agent, AgentRegistry
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.skills.management import SkillManagement
from xana.tools.builtin._process import ProcessRunner
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_SPEC = ToolSpec(
    component=ComponentSpec(kind='tool',name='skill_manage',qualified_name='builtin.skill_manage',
        description='管理外部技能。list 查看本地安装和启用范围（source 可列远程仓库）；install 先查本地，缺少才用 npx skills 安装。安装默认不启用；agents 明确指定目标 agent 后才注册可用。enable/disable 调整指定 agent 的技能。不自动给其他 agent 开放。',
        source=ComponentSource(source_type='builtin',scope='system')),
    input_schema={'type':'object','properties':{
        'action':{'type':'string','enum':['list','install','enable','disable']},
        'source':{'type':'string','description':'官方 npx skills 支持的来源；本地已有时不联网'},
        'skills':{'type':'array','items':{'type':'string'},'description':'技能名；install 可单独用 * 安装来源中全部技能'},
        'agents':{'type':'array','items':{'type':'string'},'description':'明确启用/禁用的目标 agent 名；不填只安装，不注册'}},'required':['action']},
    toolset='meta',defer=True,temporal="live",destructive=True,open_world=True,
    display=ToolDisplay(category='other',verb='管理技能',summary_args=('action','skills','agents')),
)


class SkillManageTool(Tool):
    def __init__(self, *, management: SkillManagement, agents: Mapping[str, Agent], agent_registry: AgentRegistry) -> None:
        self._management = management
        self._agents = agents
        self._registry = agent_registry
        self._runner = ProcessRunner()

    def spec(self) -> ToolSpec:
        return _SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision='allow',reason='用户聊天中的显式技能管理')

    async def cancel(self, call_id: str) -> None:
        await self._runner.cancel(call_id)

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            return await self._execute(invocation)
        except (ValueError,OSError) as exc:
            return ToolResult(ok=False,content=None,error=str(exc),failure_kind='validation',rendered=f'[skill_manage] {exc}')

    async def _execute(self, invocation: ToolInvocation) -> ToolResult:
        data=invocation.input
        action=data['action']
        names=tuple(dict.fromkeys(data.get('skills') or ()))
        targets=tuple(dict.fromkeys(data.get('agents') or ()))
        await self._registry.refresh()
        if set(targets)-self._agents.keys():
            raise ValueError('未知 agent: '+', '.join(sorted(set(targets)-self._agents.keys())))
        inventory=await self._management.inventory()
        source=data.get('source')
        if source is not None and (not isinstance(source,str) or not source.strip() or source.startswith('-')):
            raise ValueError('source 必须是非空来源，不能是命令选项')
        if action=='list':
            if source:
                return await self._npx(invocation.execution_id,source,(),list_only=True)
            bindings=self._management.bindings()
            rows=[{'name':name,'path':str(skill.spec().package.root),
                   'agents':sorted(agent for agent,entries in bindings.items() if name in entries)}
                  for name,skill in sorted(inventory.items())]
            return ToolResult(ok=True,content=rows,rendered='\n'.join(f"{r['name']} | {r['path']} | 已启用: {', '.join(r['agents']) or '无'}" for r in rows) or '没有已安装的文件技能')
        if not names or any(not isinstance(n,str) or not n or n.startswith('-') for n in names):
            raise ValueError('必须明确指定 skills')
        if '*' in names and (names!=('*',) or action!='install'):
            raise ValueError('* 只能单独用于 install；启用范围必须明确')
        if action=='install':
            missing=tuple(n for n in names if n not in inventory)
            if missing:
                if not source:
                    raise ValueError('本地缺少技能，请提供 source: '+', '.join(missing))
                result=await self._npx(invocation.execution_id,source,missing,list_only=False)
                if not result.ok:
                    return result
                if names==('*',):
                    names=tuple(result.content)
                inventory=await self._management.inventory()
                if set(names)-inventory.keys():
                    raise ValueError('安装后仍缺少指定技能: '+', '.join(sorted(set(names)-inventory.keys())))
            if targets:
                await self._management.set_enabled(names,targets,enabled=True)
            text=f"技能已就绪: {', '.join(names)}；"+(f"仅为 {', '.join(targets)} 启用" if targets else '此次未更改启用范围')
        elif action in ('enable','disable'):
            await self._management.set_enabled(names,targets,enabled=action=='enable')
            text=f"已{'启用' if action=='enable' else '禁用'} {', '.join(names)}：{', '.join(targets)}"
        else:
            raise ValueError('未知 action')
        return ToolResult(ok=True,content={'skills':names,'agents':targets},rendered=text)

    async def _npx(self, call_id: str, source: str, names: tuple[str, ...], *, list_only: bool) -> ToolResult:
        with tempfile.TemporaryDirectory(prefix='xana-skill-install-') as tmp:
            args=['npx','--yes','skills','add',source]
            args+=['--list'] if list_only else ['--skill',*names,'--agent','universal','--yes','--copy']
            outcome=await self._runner.run(call_id,argv=args,cwd=tmp,timeout_s=300,
                env={'DISABLE_TELEMETRY':'1','GIT_TERMINAL_PROMPT':'0'})
            if outcome.timed_out or outcome.exit_code:
                return ToolResult(ok=False,content=None,error='npx skills 运行失败',
                    failure_kind='timeout' if outcome.timed_out else 'execution',rendered=outcome.output[-12000:])
            if list_only:
                return ToolResult(ok=True,content=None,rendered=outcome.output[-24000:])
            installed=await self._management.adopt(Path(tmp)/'.agents/skills')
            return ToolResult(ok=True,content=installed,rendered='已安装: '+', '.join(installed))
