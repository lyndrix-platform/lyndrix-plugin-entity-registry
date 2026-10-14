import PluginApp from './PluginApp'

// Explicit global assignment — the shell looks for window.__lyndrix_plugin_<safe id>.
;(window as unknown as Record<string, unknown>)['__lyndrix_plugin_lyndrix_plugin_entity_registry'] = {
  PluginApp,
  pluginRoutes: [
    { path: '/registry', label: 'Entity Registry', icon: 'fingerprint', sidebar_visible: true },
  ],
}
