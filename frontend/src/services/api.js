import { api, auth, showError, showSuccess } from '/main.js';

const agentIntegrationApi = {
  async getAgentIntegrations(agentId) {
    return api.get(`/agents/${agentId}/integrations`);
  },

  async updateAgentIntegrations(agentId, payload) {
    return api.put(`/agents/${agentId}/integrations`, payload);
  },

  async testAgentMcp(agentId, payload) {
    return api.post(`/agents/${agentId}/mcp-test`, payload);
  },

  async testAgentRag(agentId, payload) {
    return api.post(`/agents/${agentId}/rag-test`, payload);
  },

  async bindAgentRagDatasets(agentId, payload) {
    return api.put(`/agents/${agentId}/rag/bindings`, payload);
  },

  async getAgentIntegrationAudit(agentId, limit = 20) {
    return api.get(`/agents/${agentId}/integrations/audit?limit=${encodeURIComponent(String(limit))}`);
  },
};

export {
  api,
  auth,
  showError,
  showSuccess,
  agentIntegrationApi,
};
