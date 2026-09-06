impl CodexResponsesMetadata {
    fn turn_metadata_payload(&self) -> CodexTurnMetadataPayload<'_> {
        let request_kind = self.request_kind;
        let (request_kind_value, compaction) = request_kind.map_or((None, None), |request_kind| {
            let (request_kind, compaction) = request_kind.metadata();
            (Some(request_kind), compaction)
        });
        let has_turn_identity =
            request_kind.is_none_or(CodexResponsesRequestKind::has_turn_identity);
        let has_request_identity =
            request_kind.is_some_and(CodexResponsesRequestKind::has_turn_identity);
        CodexTurnMetadataPayload {
            installation_id: has_request_identity.then_some(self.installation_id.as_str()),
            session_id: has_turn_identity.then_some(self.session_id.as_str()),
            thread_id: has_turn_identity.then_some(self.thread_id.as_str()),
            agent_name: has_turn_identity.then_some(self.agent_name.as_deref()).flatten(),
            turn_id: has_turn_identity.then_some(self.turn_id.as_deref()).flatten(),
            window_id: has_request_identity.then_some(self.window_id.as_str()),
            window_number: has_request_identity.then_some(self.window_number).flatten(),
            context_window_id: has_request_identity.then_some(self.context_window_id).flatten(),
            request_kind: request_kind_value,
            forked_from_thread_id: self.forked_from_thread_id,
            forked_from_ordinal_exclusive: self.forked_from_ordinal_exclusive,
            parent_thread_id: self.parent_thread_id,
            parent_turn_id: self.parent_turn_id.as_deref(),
            root_turn_id: self.root_turn_id.as_deref(),
            subagent_kind: self.subagent_kind.as_deref(),
            thread_source: self.thread_source.as_ref(),
            turn_trigger: self.turn_trigger.as_deref(),
            sandbox: self.sandbox.as_deref(),
            sandbox_mode: self.sandbox_mode.as_deref(),
            auto_review_enabled: self.auto_review_enabled,
            node_repl_auto_review_required: self.node_repl_auto_review_required,
            node_repl_disabled: self.node_repl_disabled,
            workspaces: non_empty_workspaces(&self.workspaces),
            tool_namespaces_info: self.tool_namespaces_info.as_ref(),
            turn_started_at_unix_ms: self.turn_started_at_unix_ms,
            history_ingest_requested: self.history_ingest_requested,
            compaction,
            extra: &self.extra,
        }
    }
}
