<%inherit file="/base.mako"/>
<%namespace file="/message.mako" import="render_msg" />

<%def name="title()">Data Manager: ${ data_manager.name | h } - ${ data_manager.description | h }</%def>

%if message:
    ${render_msg( message, status )}
%endif

<h2>Data Manager: ${ data_manager.name | h } - ${ data_manager.description | h }</h2>

%if view_only:
    <p>Not implemented</p>
%else:
    <p>Access managed data by job</p>
    
%if jobs:
<form name="jobs" action="${h.url_for()}" method="POST">
    <table class="manage-table colored" border="0" cellspacing="0" cellpadding="0" width="100%">
        <tr class="header">
            <td>Job ID</td>
            <td>User</td>
            <td>Last Update</td>
            <td>State</td>
            <td>Command Line</td>
            <td>Job Runner</td>
            <td>PID/Cluster ID</td>
        </tr>
        %for job in jobs:
                <td><a href="${ h.url_for( controller="data_manager", action="view_job", id=trans.security.encode_id( job.id ) ) }">${ job.id | h }</a></td>
                %if job.history and job.history.user:
                    <td>${job.history.user.email | h}</td>
                %else:
                    <td>anonymous</td>
                %endif
                <td>${job.update_time | h}</td>
                <td>${job.state | h}</td>
                <td>${job.command_line | h}</td>
                <td>${job.job_runner_name | h}</td>
                <td>${job.job_runner_external_id | h}</td>
            </tr>
        %endfor
    </table>
    <p/>
</form>
%else:
    <div class="infomessage">There are no jobs for this data manager.</div>
%endif

%endif