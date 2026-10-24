"""Execution choices use OpenAI's native environment schema."""
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Execution:
    kind: str = 'none'
    workspace: str | None = None
    plugin_roots: tuple[str, ...] = ()
    template_id: str | None = None
    desktop: bool = False

    def configuration(self):
        if self.kind == 'none':
            if self.workspace or self.plugin_roots or self.template_id or self.desktop:
                raise ValueError('Remote-tool work has no computer configuration')
            return {'type':'none'}
        if self.kind == 'openai_hosted':
            if self.workspace or self.plugin_roots:
                raise ValueError('Local directories belong to the Mac environment')
            config = {'type':'openai_hosted', 'desktop':{'enabled':self.desktop}}
            if self.template_id:
                config['environment_template_id'] = self.template_id
            else:
                config['container_size'] = 'small'
                config['network'] = {'access':'enabled'}
            return config
        if self.kind != 'self_hosted' or not self.workspace or self.template_id or self.desktop:
            raise ValueError('Invalid execution environment')
        for directory in (self.workspace, *self.plugin_roots):
            path = PurePosixPath(directory)
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('Mac directories must be trusted absolute paths')
        return {'type':'self_hosted', 'workspace_directory':self.workspace,
                'capability_directories':list(self.plugin_roots)}
