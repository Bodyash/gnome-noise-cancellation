import {ENGINE_NAMES} from './settings.js';

const labels = Object.freeze({
    UNAVAILABLE: 'Unavailable', DISABLED: 'Disabled', SETUP_REQUIRED: 'Setup required',
    STARTING: 'Starting', ACTIVE: 'Active', STOPPING: 'Stopping',
    RECONFIGURATION_REQUIRED: 'Select microphone again', ERROR: 'Error',
});

/** Use the controller's frozen session settings, never pending preferences. */
export function tileSubtitle(snapshot, translate = value => value) {
    const status = snapshot.state === 'ACTIVE'
        ? ({idle: 'Idle', suspending: 'Suspending', resuming: 'Resuming'}[snapshot.activity] ?? 'Active')
        : labels[snapshot.state];
    return `${ENGINE_NAMES[snapshot.processing.backend]} · ${translate(status)}`;
}
