/*
 * Chat panel behaviour.
 *
 * Deliberately thin: it posts the clinician's words to OpenMRS, renders what comes back, and
 * shows a confirmation step when the assistant says one is required. It holds no knowledge of
 * tasks, FHIR, or endpoints - all of that lives on the agent service, and the browser never talks
 * to it directly (see ChatRelayController).
 *
 * The confirmation gate is enforced on the server, not here: this UI only surfaces it. Bypassing
 * this button changes nothing, because the pending action lives in the agent's conversation
 * buffer and is only executed when a confirming turn arrives.
 */
var agentConversationId = null;
var agentBusy = false;

/*
 * The full-screen page sets this true; the dashboard widget leaves it false. Focusing an input
 * inside a widget on page load scrolls the browser down to it, which would drag the clinician
 * away from the patient summary they actually opened the page to read.
 */
var agentAutoFocus = (typeof agentAutoFocus === 'undefined') ? false : agentAutoFocus;

function agentEndpoint(name) {
    return '/' + OPENMRS_CONTEXT_PATH + '/module/agentgateway/' + name;
}

function agentEscape(text) {
    return jQuery('<div/>').text(text === null || text === undefined ? '' : text).html();
}

function agentAppend(text, cssClass) {
    var messages = jQuery('#agentMessages');
    messages.append('<div class="agent-message ' + cssClass + '">' + agentEscape(text).replace(/\n/g, '<br/>') + '</div>');
    messages.scrollTop(messages[0].scrollHeight);
}

function agentSetBusy(busy) {
    agentBusy = busy;
    jQuery('#agentSendButton').prop('disabled', busy);
    jQuery('#agentInput').prop('disabled', busy);
}

function agentShowPending(pendingAction) {
    if (!pendingAction) {
        jQuery('#agentPending').hide();
        jQuery('#agentPendingSummary').empty();
        return;
    }

    var html = '<p>' + agentEscape(pendingAction.summary) + '</p>';
    var operations = pendingAction.operations || [];
    if (operations.length) {
        html += '<ul class="agent-pending-ops">';
        for (var i = 0; i < operations.length; i++) {
            html += '<li>' + agentEscape(operations[i].summary || (operations[i].method + ' ' + operations[i].path)) + '</li>';
        }
        html += '</ul>';
    }
    jQuery('#agentPendingSummary').html(html);
    jQuery('#agentPending').show();
}

function agentPost(message, echo) {
    if (agentBusy) {
        return;
    }
    if (echo) {
        agentAppend(message, 'agent-message-user');
    }
    agentSetBusy(true);
    agentShowPending(null);

    jQuery.post(agentEndpoint('chat.form'), {
        message: message,
        conversationId: agentConversationId,
        patientUuid: agentPatientUuid
    }).done(function (response) {
        agentConversationId = response.conversationId || agentConversationId;
        agentAppend(response.reply || "L'assistant n'a rien renvoye.", 'agent-message-bot');
        if (response.state === 'awaiting_confirmation') {
            agentShowPending(response.pending_action);
        }
    }).fail(function (xhr) {
        var reply = "L'assistant est momentanement indisponible.";
        if (xhr.status === 403) {
            reply = "Vous n'avez pas l'autorisation d'effectuer cette action.";
        }
        agentAppend(reply, 'agent-message-bot agent-message-error');
    }).always(function () {
        agentSetBusy(false);
        jQuery('#agentInput').focus();
    });
}

function agentSend(event) {
    event.preventDefault();
    var input = jQuery('#agentInput');
    var message = jQuery.trim(input.val());
    if (!message) {
        return false;
    }
    input.val('');
    agentPost(message, true);
    return false;
}

/*
 * "Confirm" and "cancel" are ordinary conversation turns rather than a separate endpoint, so
 * there is exactly one path into the agent and one place where a write can be authorised.
 */
function agentConfirm() {
    agentShowPending(null);
    agentAppend('Confirmer', 'agent-message-user');
    agentPost('oui, je confirme', false);
}

function agentCancel() {
    agentShowPending(null);
    agentAppend('Annuler', 'agent-message-user');
    agentPost('non, annuler', false);
}

jQuery(function () {
    // Read the flag here rather than at load time: this file is included in the page head, so
    // the inline script that sets it has not run yet when the file itself is evaluated.
    if (agentAutoFocus) {
        jQuery('#agentInput').focus();
    }
});
