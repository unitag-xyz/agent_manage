# syntax=docker/dockerfile:1.7
ARG OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw@sha256:2f5ce8848a1a69b3c460622e566cb9395da9fd18d7ef7b038cd8e2c4f195decf
FROM ${OPENCLAW_IMAGE}

ARG OPENCLAW_VERSION=2026.7.1-1
ARG TEMPLATE_IDENTIFY
ARG AGENT_MANAGER_VERSION=0.5.0
ARG LAYOUT_PROTOCOL_VERSION=2
ARG LARKSUITE_CLI_VERSION=1.0.93
ARG WEIXIN_PLUGIN_VERSION=2.4.8
ARG QQBOT_PLUGIN_VERSION=2026.7.1
ARG FEISHU_PLUGIN_VERSION=2026.7.1
ARG NGINX_PORT=80
ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security

LABEL io.dola.unitag.template-identify="${TEMPLATE_IDENTIFY}" \
      io.dola.unitag.openclaw-version="${OPENCLAW_VERSION}" \
      io.dola.unitag.agent-manager-version="${AGENT_MANAGER_VERSION}" \
      io.dola.unitag.layout-protocol-version="${LAYOUT_PROTOCOL_VERSION}" \
      io.dola.unitag.larksuite-cli-version="${LARKSUITE_CLI_VERSION}" \
      io.dola.unitag.weixin-plugin-version="${WEIXIN_PLUGIN_VERSION}" \
      io.dola.unitag.qqbot-plugin-version="${QQBOT_PLUGIN_VERSION}" \
      io.dola.unitag.feishu-plugin-version="${FEISHU_PLUGIN_VERSION}" \
      io.dola.unitag.nginx-port="${NGINX_PORT}"

USER root
ENV PYTHONPATH=/opt/unitag/agent-manager
RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=30 install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default \
    && npm install -g "@larksuite/cli@${LARKSUITE_CLI_VERSION}" --ignore-scripts --no-audit --no-fund
ADD --checksum=sha256:10031849a1884bf9165cb01de47102b4847fba81bb9eb76f4168125ea9746c51 \
    "https://registry.npmmirror.com/-/binary/lark-cli/v${LARKSUITE_CLI_VERSION}/lark-cli-${LARKSUITE_CLI_VERSION}-linux-amd64.tar.gz" \
    /tmp/lark-cli.tar.gz
RUN cli_dir="$(npm root -g)/@larksuite/cli" \
    && archive="lark-cli-${LARKSUITE_CLI_VERSION}-linux-amd64.tar.gz" \
    && expected="$(awk -v name="${archive}" '$2 == name { print $1 }' "${cli_dir}/checksums.txt")" \
    && test -n "${expected}" \
    && printf '%s  %s\n' "${expected}" /tmp/lark-cli.tar.gz | sha256sum --check --strict \
    && mkdir -p "${cli_dir}/bin" \
    && tar -xzf /tmp/lark-cli.tar.gz -C /tmp \
    && install -m 0755 /tmp/lark-cli "${cli_dir}/bin/lark-cli" \
    && rm -f /tmp/lark-cli.tar.gz /tmp/lark-cli

COPY agent_manage /opt/unitag/agent-manager/agent_manage
COPY scripts /opt/unitag/agent-manager/scripts
COPY container-image/nginx.conf /opt/unitag/nginx/nginx.conf

RUN test -n "${TEMPLATE_IDENTIFY}"
COPY container-image/templates/${TEMPLATE_IDENTIFY}.zip /home/node/template/${TEMPLATE_IDENTIFY}.zip
RUN mkdir -p /opt/unitag/openclaw-seed \
    && mkdir -p /home/node/.openclaw/templates \
    && cp /home/node/template/${TEMPLATE_IDENTIFY}.zip /home/node/.openclaw/templates/${TEMPLATE_IDENTIFY}.zip \
    && printf '{"gateway":{"mode":"local","auth":{"mode":"token"}}}\n' > /home/node/.openclaw/openclaw.json \
    && chown -R node:node /home/node/template /home/node/.openclaw /opt/unitag/openclaw-seed

USER node
RUN UNITAG_AGENT_HOOKS_DEFER=1 python3 /opt/unitag/agent-manager/scripts/agentctl.py add-agent \
      --template-name "${TEMPLATE_IDENTIFY}" \
      --workspace-root /home/node/.openclaw/data \
    && openclaw plugins install "@tencent-weixin/openclaw-weixin@${WEIXIN_PLUGIN_VERSION}" \
    && openclaw plugins install "@openclaw/qqbot@${QQBOT_PLUGIN_VERSION}" \
    && openclaw plugins install "@openclaw/feishu@${FEISHU_PLUGIN_VERSION}" \
    && openclaw config set plugins.entries.openclaw-weixin.enabled true \
    && openclaw config set tools.agentToAgent.enabled true --strict-json \
    && openclaw config set tools.sessions.visibility '"all"' --strict-json \
    && openclaw config set agents.defaults.subagents.allowAgents '["*"]' --strict-json \
    && openclaw config set update.checkOnStart false \
    && cp -a /home/node/.openclaw/. /opt/unitag/openclaw-seed/

USER root
RUN python3 -m agent_manage.seed \
      --seed /opt/unitag/openclaw-seed \
      --validate-only \
    && chown -R node:node /opt/unitag/openclaw-seed
COPY container-image/unitag-openclaw-entrypoint.sh /usr/local/bin/unitag-openclaw-entrypoint
RUN chmod 0755 /usr/local/bin/unitag-openclaw-entrypoint \
    && chmod 0644 /opt/unitag/nginx/nginx.conf

USER node
EXPOSE ${NGINX_PORT} 18789
ENTRYPOINT ["/usr/local/bin/unitag-openclaw-entrypoint"]
CMD ["node", "openclaw.mjs", "gateway", "--bind", "lan"]
