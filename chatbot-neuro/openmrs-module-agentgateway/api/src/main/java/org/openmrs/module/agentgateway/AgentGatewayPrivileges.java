package org.openmrs.module.agentgateway;

import org.openmrs.api.APIAuthenticationException;
import org.openmrs.api.context.Context;

/**
 * Privilege names gating the conversational agent.
 * <p>
 * All three are "App:"-prefixed on purpose. The Reference Application auto-grants every plain
 * (non "App:"/"Task:") privilege to all roles, so an unprefixed privilege would gate nothing -
 * the same trap documented at length in {@code PatientviewPrivileges}. These three are the real
 * boundary and must be assigned to roles explicitly.
 * <p>
 * {@link #CHAT_WRITE} is an <em>extra</em> gate layered on top of the user's ordinary
 * resource-level privileges, never a replacement for them: a surgeon whose OpenMRS account can
 * already create patients by hand still cannot have the agent do it unless they also hold this
 * privilege. That is what lets an administrator switch the chat's write capability off
 * hospital-wide without touching anybody's clinical permissions.
 */
public final class AgentGatewayPrivileges {

	/** Open the chat panel and have read-only lookups executed. */
	public static final String CHAT_USE = "App: agentgateway.chat.use";

	/** Allow a confirmed write to be executed on this user's behalf by the agent. */
	public static final String CHAT_WRITE = "App: agentgateway.chat.write";

	/** Review the operation log and attempt a rollback. Administrators only. */
	public static final String ROLLBACK = "App: agentgateway.rollback";

	private AgentGatewayPrivileges() {
	}

	public static void requireChatUse() {
		require(CHAT_USE);
	}

	public static void requireChatWrite() {
		require(CHAT_WRITE);
	}

	public static void requireRollback() {
		require(ROLLBACK);
	}

	private static void require(String privilege) {
		if (!Context.hasPrivilege(privilege)) {
			throw new APIAuthenticationException("Requires privilege: " + privilege);
		}
	}
}
