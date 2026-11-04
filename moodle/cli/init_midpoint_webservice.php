<?php
/**
 * Idempotent CLI: enable REST web services for MidPoint cohort provisioning.
 *
 * Creates role, service user, external service, functions, and a permanent token.
 * Prints TOKEN=<value> on stdout for compose init to capture.
 *
 * Usage (inside Moodle container):
 *   php /path/to/init_midpoint_webservice.php
 */
define('CLI_SCRIPT', true);

$moodledir = getenv('MOODLE_DIR') ?: '/bitnami/moodle';
require($moodledir . '/config.php');
require_once($CFG->libdir . '/clilib.php');
require_once($CFG->libdir . '/externallib.php');
require_once($CFG->dirroot . '/user/lib.php');
require_once($CFG->dirroot . '/webservice/lib.php');

$service_shortname = 'midpoint_invite';
$service_name = 'MidPoint Invite Integration';
$username = getenv('MOODLE_WS_USERNAME') ?: 'midpoint-provisioner';
$password = getenv('MOODLE_WS_PASSWORD') ?: 'MidPointWsPass123!';
$email = getenv('MOODLE_WS_EMAIL') ?: 'midpoint-provisioner@example.com';
$firstname = 'MidPoint';
$lastname = 'Provisioner';
$rolename = 'midpoint_provisioner';
$role_shortname = 'midpoint_provisioner';

$functions = [
    'core_webservice_get_site_info',
    'core_user_get_users',
    'core_user_get_users_by_field',
    'core_user_create_users',
    'core_user_update_users',
    'core_cohort_get_cohorts',
    'core_cohort_search_cohorts',
    'core_cohort_create_cohorts',
    'core_cohort_update_cohorts',
    'core_cohort_delete_cohorts',
    'core_cohort_add_cohort_members',
    'core_cohort_delete_cohort_members',
    'core_cohort_get_cohort_members',
];

$capabilities = [
    'moodle/user:create',
    'moodle/user:update',
    'moodle/user:viewalldetails',
    'moodle/user:viewdetails',
    'moodle/user:viewhiddendetails',
    'moodle/cohort:manage',
    'moodle/cohort:view',
    'moodle/cohort:assign',
    'moodle/site:viewuseridentity',
    'webservice/rest:use',
];

cli_heading('MidPoint Moodle webservice init');

set_config('enablewebservices', 1);
$protocols = array_filter(array_map('trim', explode(',', get_config('core', 'webserviceprotocols') ?: '')));
if (!in_array('rest', $protocols, true)) {
    $protocols[] = 'rest';
}
set_config('webserviceprotocols', implode(',', $protocols));

$systemcontext = context_system::instance();
$role = $DB->get_record('role', ['shortname' => $role_shortname]);
if (!$role) {
    $roleid = create_role($rolename, $role_shortname, 'Web service role for MidPoint cohort provisioning');
    $role = $DB->get_record('role', ['id' => $roleid], '*', MUST_EXIST);
    cli_writeln("Created role {$role_shortname}");
} else {
    $roleid = $role->id;
    cli_writeln("Role {$role_shortname} already exists");
}
set_role_contextlevels($roleid, [CONTEXT_SYSTEM]);
foreach ($capabilities as $cap) {
    if (!$DB->record_exists('capabilities', ['name' => $cap])) {
        cli_writeln("Skip missing capability {$cap}");
        continue;
    }
    assign_capability($cap, CAP_ALLOW, $roleid, $systemcontext->id, true);
}
$systemcontext->mark_dirty();

$user = $DB->get_record('user', ['username' => $username, 'deleted' => 0]);
if (!$user) {
    $newuser = (object) [
        'auth' => 'manual',
        'confirmed' => 1,
        'mnethostid' => $CFG->mnet_localhost_id,
        'username' => $username,
        'password' => $password,
        'firstname' => $firstname,
        'lastname' => $lastname,
        'email' => $email,
        'mailformat' => 1,
        'lang' => 'en',
        'timezone' => '99',
    ];
    $userid = user_create_user($newuser, true, false);
    cli_writeln("Created user {$username}");
} else {
    $userid = $user->id;
    user_update_user((object) [
        'id' => $userid,
        'firstname' => $firstname,
        'lastname' => $lastname,
        'email' => $email,
        'password' => $password,
    ], false, false);
    cli_writeln("Updated user {$username}");
}
$user = $DB->get_record('user', ['id' => $userid], '*', MUST_EXIST);
role_assign($roleid, $user->id, $systemcontext->id);

$service = $DB->get_record('external_services', ['shortname' => $service_shortname]);
if (!$service) {
    $service = (object) [
        'name' => $service_name,
        'shortname' => $service_shortname,
        'enabled' => 1,
        'requiredcapability' => '',
        'restrictedusers' => 1,
        'downloadfiles' => 0,
        'uploadfiles' => 0,
        'timecreated' => time(),
        'timemodified' => time(),
    ];
    $service->id = $DB->insert_record('external_services', $service);
    cli_writeln("Created external service {$service_shortname}");
} else {
    $service->enabled = 1;
    $service->restrictedusers = 1;
    $service->timemodified = time();
    $DB->update_record('external_services', $service);
    cli_writeln("External service {$service_shortname} already exists");
}

foreach ($functions as $fname) {
    if (!$DB->record_exists('external_functions', ['name' => $fname])) {
        cli_writeln("Skip missing WS function {$fname}");
        continue;
    }
    if (!$DB->record_exists('external_services_functions', [
        'externalserviceid' => $service->id,
        'functionname' => $fname,
    ])) {
        $DB->insert_record('external_services_functions', (object) [
            'externalserviceid' => $service->id,
            'functionname' => $fname,
        ]);
        cli_writeln("Added function {$fname}");
    }
}

if (!$DB->record_exists('external_services_users', [
    'externalserviceid' => $service->id,
    'userid' => $user->id,
])) {
    $DB->insert_record('external_services_users', (object) [
        'externalserviceid' => $service->id,
        'userid' => $user->id,
        'timecreated' => time(),
    ]);
    cli_writeln('Authorized user on service');
}

$existing = $DB->get_record('external_tokens', [
    'userid' => $user->id,
    'externalserviceid' => $service->id,
    'tokentype' => EXTERNAL_TOKEN_PERMANENT,
]);
if ($existing) {
    $token = $existing->token;
    cli_writeln('Reusing existing permanent token');
} else {
    $token = external_generate_token(
        EXTERNAL_TOKEN_PERMANENT,
        $service,
        $user->id,
        $systemcontext
    );
    cli_writeln('Generated new permanent token');
}

purge_all_caches();

echo "TOKEN={$token}\n";
echo "SERVICE={$service_shortname}\n";
echo "USER={$username}\n";
cli_writeln('Done.');
