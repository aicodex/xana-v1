"""Default-agent-only publisher: review portable payload first, build without changing the source project."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.config.paths import project_home, xana_home
from xana.config.project_config import read_project_config
from xana.policy.contracts import PermissionResult
from xana.publishing.package import inspect_package, publish
from xana.runtime.projects import project_info
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

_SPEC=ToolSpec(
    component=ComponentSpec(kind='tool',name='agent_publish',qualified_name='builtin.agent_publish',
        description='发布独立 agent 能力包。仅默认 chat 可用。先 inspect 审阅明确选择的项目文件、记忆、agent、技能和依赖问题；build 生成标准 SKILL.md 能力包、独立 xana 运行核 wheel 和一键安装入口。不会修改原项目或停止服务；凭据和外部绝对路径会阻止发布。',
        source=ComponentSource(source_type='builtin',scope='system')),
    temporal="static",toolset='publishing',allowed_agents=('chat',),defer=True,destructive=True,
    input_schema={'type':'object','properties':{
        'action':{'type':'string','enum':['inspect','build']},
        'project':{'type':'string','description':'源 xana 项目 id'},
        'name':{'type':'string','description':'发布包名，如 video-assistant'},
        'version':{'type':'string','description':'x.y.z'},
        'description':{'type':'string'},
        'agents':{'type':'array','items':{'type':'string'},'description':'额外打包的依赖 agent 名（项目级优先于用户级）'},
        'tools':{'type':'array','items':{'type':'string'},'description':'额外打包的派生工具名，包含完整工具目录'},
        'entry_agent':{'type':'string','default':'chat'},
        'include':{'type':'array','items':{'type':'string'},'description':'明确选择的项目内相对文件/目录，如 AGENTS.md、memory、.xana/agents、video-gen'},
        'runtime_url':{'type':'string','description':'可选：仓库分发时独立运行核 wheel 的 HTTPS 下载地址'},
        'python_dependencies':{'type':'array','items':{'type':'string'}},
        'executables':{'type':'array','items':{'type':'string'}},
        'settings':{'type':'object','additionalProperties':{'type':'string'},'description':'业务环境变量名到配置提示，例如 COMFYUI_API_URL'},
        'output':{'type':'string','description':'build 输出 zip 路径；缺省放到项目私有 releases 目录'}},
        'required':['action','project','include']},
    display=ToolDisplay(category='other',verb='发布 Agent',summary_args=('action','project','name')),
)


class AgentPublishTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision='allow' if context.agent_name=='chat' else 'deny',reason='只有默认 chat agent 可以发布')

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        if context.agent_name!='chat':
            return ToolResult(ok=False,content=None,failure_kind='policy_forbidden',error='只有默认 chat agent 可以发布')
        try:
            data=invocation.input
            info=project_info(data['project'])
            includes=data['include']
            entry=data.get('entry_agent','chat')
            if not includes or not all(isinstance(p,str) and p for p in includes):
                raise ValueError('必须明确选择非空 include')
            # Capture only included agents' explicit bindings. Global skill packages are copied in full.
            from xana.skills.markdown_loader import FileSkillLoader
            binding_file=project_home(data['project'])/'skill_bindings.json'
            bindings=json.loads(binding_file.read_text()) if binding_file.exists() else {}
            skills,package_sources={},{}
            selected={'chat',entry,*(data.get('agents') or [])}
            for agent in selected-{'chat'}:
                if Path(agent).name!=agent:
                    raise ValueError('无效 agent 名')
                local=info.root/'.xana/agents'/agent
                source=local if (local/'AGENT.md').is_file() else xana_home()/'agents'/agent
                if (source/'AGENT.md').is_file():
                    package_sources['.xana/agents/'+agent]=source
                else:
                    raise ValueError(f'依赖 agent 缺失: {agent}')
            for name in data.get('tools') or []:
                if Path(name).name!=name:
                    raise ValueError('无效工具名')
                local=info.root/'.xana/tools'/name
                source=local if (local/'TOOL.md').is_file() else xana_home()/'tools'/name
                if not (source/'TOOL.md').is_file():
                    raise ValueError(f'派生工具缺失: {name}')
                package_sources['.xana/tools/'+name]=source
            for path in includes:
                for agent_doc in (info.root/path).glob('*/AGENT.md') if (info.root/path).is_dir() else ():
                    selected.add(agent_doc.parent.name)
                if path.endswith('/AGENT.md'):
                    selected.add(Path(path).parent.name)
            for agent,items in bindings.items():
                if agent not in selected:
                    continue
                paths=[]
                for path in items.values():
                    resolved=Path(path).resolve()
                    skill=await FileSkillLoader().load(str(resolved))
                    name=skill.spec().component.name
                    prefix='.agents/skills/'+name
                    if prefix in package_sources and package_sources[prefix]!=resolved.parent:
                        raise ValueError(f'发布范围存在不同来源的同名技能: {name}')
                    package_sources[prefix]=resolved.parent
                    paths.append(prefix+'/SKILL.md')
                skills[agent]=paths
            report=await asyncio.to_thread(inspect_package,info.root,includes,entry,package_sources)
            report['skills']=skills
            if data['action']=='inspect':
                return ToolResult(ok=True,content=report,rendered=json.dumps(report,ensure_ascii=False,indent=2))
            if data['action']!='build':
                raise ValueError('未知发布操作')
            output=Path(data.get('output') or project_home(data['project'])/'releases'/f"{data['name']}-{data['version']}.zip").expanduser()
            path=await asyncio.to_thread(publish,info.root,name=data['name'],version=data['version'],
                description=data['description'],entry_agent=entry,includes=includes,
                agent_defaults=info.agent_defaults,skills=skills,output=output,package_sources=package_sources,
                python_dependencies=data.get("python_dependencies"),executables=data.get("executables"),settings=data.get("settings"),runtime_url=data.get("runtime_url"))
            return ToolResult(ok=True,content={'path':str(path)},rendered=f'已生成发布包: {path}。解压后 python install.py；或使用 xana agents add。')
        except (ValueError,OSError,LookupError,KeyError) as exc:
            return ToolResult(ok=False,content=None,error=str(exc),failure_kind='validation',rendered=f'[agent_publish] {exc}')
