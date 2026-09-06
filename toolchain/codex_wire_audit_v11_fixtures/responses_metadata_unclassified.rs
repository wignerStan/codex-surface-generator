impl CodexResponsesMetadata {
    fn turn_metadata_payload(&self) -> CodexTurnMetadataPayload<'_> {
        CodexTurnMetadataPayload {
            turn_id: new_helper(self.turn_id.as_deref()),
            extra: &self.extra,
        }
    }
}
