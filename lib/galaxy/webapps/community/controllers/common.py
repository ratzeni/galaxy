import os, string, socket, logging, simplejson, binascii, tempfile
from time import gmtime, strftime
from datetime import *
from galaxy.tools import *
from galaxy.util.odict import odict
from galaxy.util.json import from_json_string, to_json_string
import galaxy.util.shed_util_common as suc
from galaxy.web.base.controllers.admin import *
from galaxy.webapps.community import model
from galaxy.model.orm import and_
from galaxy.model.item_attrs import UsesItemRatings

from galaxy import eggs
eggs.require('mercurial')
from mercurial import hg, ui, commands

log = logging.getLogger( __name__ )

new_repo_email_alert_template = """
Repository name:       ${repository_name}
Revision:              ${revision}
Change description:
${description}

Uploaded by:           ${username}
Date content uploaded: ${display_date}

${content_alert_str}

-----------------------------------------------------------------------------
This change alert was sent from the Galaxy tool shed hosted on the server
"${host}"
-----------------------------------------------------------------------------
You received this alert because you registered to receive email when
new repositories were created in the Galaxy tool shed named "${host}".
-----------------------------------------------------------------------------
"""

email_alert_template = """
Repository name:       ${repository_name}
Revision: ${revision}
Change description:
${description}

Changed by:     ${username}
Date of change: ${display_date}

${content_alert_str}

-----------------------------------------------------------------------------
This change alert was sent from the Galaxy tool shed hosted on the server
"${host}"
-----------------------------------------------------------------------------
You received this alert because you registered to receive email whenever
changes were made to the repository named "${repository_name}".
-----------------------------------------------------------------------------
"""

contact_owner_template = """
GALAXY TOOL SHED REPOSITORY MESSAGE
------------------------

The user '${username}' sent you the following message regarding your tool shed
repository named '${repository_name}'.  You can respond by sending a reply to
the user's email address: ${email}.
-----------------------------------------------------------------------------
${message}
-----------------------------------------------------------------------------
This message was sent from the Galaxy Tool Shed instance hosted on the server
'${host}'
"""

malicious_error = "  This changeset cannot be downloaded because it potentially produces malicious behavior or contains inappropriate content."
malicious_error_can_push = "  Correct this changeset as soon as possible, it potentially produces malicious behavior or contains inappropriate content."

class ItemRatings( UsesItemRatings ):
    """Overrides rate_item method since we also allow for comments"""
    def rate_item( self, trans, user, item, rating, comment='' ):
        """ Rate an item. Return type is <item_class>RatingAssociation. """
        item_rating = self.get_user_item_rating( trans.sa_session, user, item, webapp_model=trans.model )
        if not item_rating:
            # User has not yet rated item; create rating.
            item_rating_assoc_class = self._get_item_rating_assoc_class( item, webapp_model=trans.model )
            item_rating = item_rating_assoc_class()
            item_rating.user = trans.user
            item_rating.set_item( item )
            item_rating.rating = rating
            item_rating.comment = comment
            trans.sa_session.add( item_rating )
            trans.sa_session.flush()
        elif item_rating.rating != rating or item_rating.comment != comment:
            # User has previously rated item; update rating.
            item_rating.rating = rating
            item_rating.comment = comment
            trans.sa_session.add( item_rating )
            trans.sa_session.flush()
        return item_rating

def add_tool_versions( trans, id, repository_metadata, changeset_revisions ):
    # Build a dictionary of { 'tool id' : 'parent tool id' } pairs for each tool in repository_metadata.
    metadata = repository_metadata.metadata
    tool_versions_dict = {}
    for tool_dict in metadata.get( 'tools', [] ):
        # We have at least 2 changeset revisions to compare tool guids and tool ids.
        parent_id = suc.get_parent_id( trans,
                                       id,
                                       tool_dict[ 'id' ],
                                       tool_dict[ 'version' ],
                                       tool_dict[ 'guid' ],
                                       changeset_revisions )
        tool_versions_dict[ tool_dict[ 'guid' ] ] = parent_id
    if tool_versions_dict:
        repository_metadata.tool_versions = tool_versions_dict
        trans.sa_session.add( repository_metadata )
        trans.sa_session.flush()
def changeset_is_malicious( trans, id, changeset_revision, **kwd ):
    """Check the malicious flag in repository metadata for a specified change set"""
    repository_metadata = suc.get_repository_metadata_by_changeset_revision( trans, id, changeset_revision )
    if repository_metadata:
        return repository_metadata.malicious
    return False
def changeset_revision_reviewed_by_user( trans, user, repository, changeset_revision ):
    """Determine if the current changeset revision has been reviewed by the current user."""
    for review in repository.reviews:
        if review.changeset_revision == changeset_revision and review.user == user:
            return True
    return False
def check_file_contents( trans ):
    # See if any admin users have chosen to receive email alerts when a repository is updated.
    # If so, the file contents of the update must be checked for inappropriate content.
    admin_users = trans.app.config.get( "admin_users", "" ).split( "," )
    for repository in trans.sa_session.query( trans.model.Repository ) \
                                      .filter( trans.model.Repository.table.c.email_alerts != None ):
        email_alerts = from_json_string( repository.email_alerts )
        for user_email in email_alerts:
            if user_email in admin_users:
                return True
    return False
def get_category( trans, id ):
    """Get a category from the database"""
    return trans.sa_session.query( trans.model.Category ).get( trans.security.decode_id( id ) )
def get_category_by_name( trans, name ):
    """Get a category from the database via name"""
    try:
        return trans.sa_session.query( trans.model.Category ).filter_by( name=name ).one()
    except sqlalchemy.orm.exc.NoResultFound:
        return None
def get_categories( trans ):
    """Get all categories from the database"""
    return trans.sa_session.query( trans.model.Category ) \
                           .filter( trans.model.Category.table.c.deleted==False ) \
                           .order_by( trans.model.Category.table.c.name ) \
                           .all()
def get_component( trans, id ):
    """Get a component from the database"""
    return trans.sa_session.query( trans.model.Component ).get( trans.security.decode_id( id ) )
def get_component_by_name( trans, name ):
    return trans.sa_session.query( trans.app.model.Component ) \
                           .filter( trans.app.model.Component.table.c.name==name ) \
                           .first()
def get_component_review( trans, id ):
    """Get a component_review from the database"""
    return trans.sa_session.query( trans.model.ComponentReview ).get( trans.security.decode_id( id ) )
def get_component_review_by_repository_review_id_component_id( trans, repository_review_id, component_id ):
    """Get a component_review from the database via repository_review_id and component_id"""
    return trans.sa_session.query( trans.model.ComponentReview ) \
                           .filter( and_( trans.model.ComponentReview.table.c.repository_review_id == trans.security.decode_id( repository_review_id ),
                                          trans.model.ComponentReview.table.c.component_id == trans.security.decode_id( component_id ) ) ) \
                           .first()
def get_components( trans ):
    return trans.sa_session.query( trans.app.model.Component ) \
                           .order_by( trans.app.model.Component.name ) \
                           .all()
def get_latest_repository_metadata( trans, decoded_repository_id ):
    """Get last metadata defined for a specified repository from the database"""
    return trans.sa_session.query( trans.model.RepositoryMetadata ) \
                           .filter( trans.model.RepositoryMetadata.table.c.repository_id == decoded_repository_id ) \
                           .order_by( trans.model.RepositoryMetadata.table.c.id.desc() ) \
                           .first()
def get_previous_repository_reviews( trans, repository, changeset_revision ):
    """Return an ordered dictionary of repository reviews up to and including the received changeset revision."""
    repo = hg.repository( suc.get_configured_ui(), repository.repo_path( trans.app ) )
    reviewed_revision_hashes = [ review.changeset_revision for review in repository.reviews ]
    previous_reviews_dict = odict()
    for changeset in suc.reversed_upper_bounded_changelog( repo, changeset_revision ):
        previous_changeset_revision = str( repo.changectx( changeset ) )
        if previous_changeset_revision in reviewed_revision_hashes:
            previous_rev, previous_changeset_revision_label = get_rev_label_from_changeset_revision( repo, previous_changeset_revision )
            revision_reviews = get_reviews_by_repository_id_changeset_revision( trans,
                                                                                trans.security.encode_id( repository.id ),
                                                                                previous_changeset_revision )
            previous_reviews_dict[ previous_changeset_revision ] = dict( changeset_revision_label=previous_changeset_revision_label,
                                                                         reviews=revision_reviews )
    return previous_reviews_dict
def get_repository_by_name( trans, name ):
    """Get a repository from the database via name"""
    return trans.sa_session.query( trans.model.Repository ).filter_by( name=name ).one()
def get_repository_metadata_revisions_for_review( repository, reviewed=True ):
    repository_metadata_revisions = []
    metadata_changeset_revision_hashes = []
    if reviewed:
        for metadata_revision in repository.metadata_revisions:
            metadata_changeset_revision_hashes.append( metadata_revision.changeset_revision )
        for review in repository.reviews:
            if review.changeset_revision in metadata_changeset_revision_hashes:
                rmcr_hashes = [ rmr.changeset_revision for rmr in repository_metadata_revisions ]
                if review.changeset_revision not in rmcr_hashes:
                    repository_metadata_revisions.append( review.repository_metadata )
    else:
        for review in repository.reviews:
            if review.changeset_revision not in metadata_changeset_revision_hashes:
                metadata_changeset_revision_hashes.append( review.changeset_revision )
        for metadata_revision in repository.metadata_revisions:
            if metadata_revision.changeset_revision not in metadata_changeset_revision_hashes:
                repository_metadata_revisions.append( metadata_revision )
    return repository_metadata_revisions
def get_rev_label_changeset_revision_from_repository_metadata( trans, repository_metadata, repository=None ):
    if repository is None:
        repository = repository_metadata.repository
    repo = hg.repository( suc.get_configured_ui(), repository.repo_path( trans.app ) )
    changeset_revision = repository_metadata.changeset_revision
    ctx = suc.get_changectx_for_changeset( repo, changeset_revision )
    if ctx:
        rev = '%04d' % ctx.rev()
        label = "%s:%s" % ( str( ctx.rev() ), changeset_revision )
    else:
        rev = '-1'
        label = "-1:%s" % changeset_revision
    return rev, label, changeset_revision
def get_rev_label_from_changeset_revision( repo, changeset_revision ):
    ctx = suc.get_changectx_for_changeset( repo, changeset_revision )
    if ctx:
        rev = '%04d' % ctx.rev()
        label = "%s:%s" % ( str( ctx.rev() ), changeset_revision )
    else:
        rev = '-1'
        label = "-1:%s" % changeset_revision
    return rev, label
def get_reversed_changelog_changesets( repo ):
    reversed_changelog = []
    for changeset in repo.changelog:
        reversed_changelog.insert( 0, changeset )
    return reversed_changelog
def get_review( trans, id ):
    """Get a repository_review from the database via id"""
    return trans.sa_session.query( trans.model.RepositoryReview ).get( trans.security.decode_id( id ) )
def get_review_by_repository_id_changeset_revision_user_id( trans, repository_id, changeset_revision, user_id ):
    """Get a repository_review from the database via repository id, changeset_revision and user_id"""
    return trans.sa_session.query( trans.model.RepositoryReview ) \
                           .filter( and_( trans.model.RepositoryReview.repository_id == trans.security.decode_id( repository_id ),
                                          trans.model.RepositoryReview.changeset_revision == changeset_revision,
                                          trans.model.RepositoryReview.user_id == trans.security.decode_id( user_id ) ) ) \
                           .first()
def get_reviews_by_repository_id_changeset_revision( trans, repository_id, changeset_revision ):
    """Get all repository_reviews from the database via repository id and changeset_revision"""
    return trans.sa_session.query( trans.model.RepositoryReview ) \
                           .filter( and_( trans.model.RepositoryReview.repository_id == trans.security.decode_id( repository_id ),
                                          trans.model.RepositoryReview.changeset_revision == changeset_revision ) ) \
                           .all()
def get_revision_label( trans, repository, changeset_revision ):
    """
    Return a string consisting of the human read-able 
    changeset rev and the changeset revision string.
    """
    repo = hg.repository( suc.get_configured_ui(), repository.repo_path( trans.app ) )
    ctx = suc.get_changectx_for_changeset( repo, changeset_revision )
    if ctx:
        return "%s:%s" % ( str( ctx.rev() ), changeset_revision )
    else:
        return "-1:%s" % changeset_revision
def get_user( trans, id ):
    """Get a user from the database by id"""
    return trans.sa_session.query( trans.model.User ).get( trans.security.decode_id( id ) )
def handle_email_alerts( trans, repository, content_alert_str='', new_repo_alert=False, admin_only=False ):
    # There are 2 complementary features that enable a tool shed user to receive email notification:
    # 1. Within User Preferences, they can elect to receive email when the first (or first valid)
    #    change set is produced for a new repository.
    # 2. When viewing or managing a repository, they can check the box labeled "Receive email alerts"
    #    which caused them to receive email alerts when updates to the repository occur.  This same feature
    #    is available on a per-repository basis on the repository grid within the tool shed.
    #
    # There are currently 4 scenarios for sending email notification when a change is made to a repository:
    # 1. An admin user elects to receive email when the first change set is produced for a new repository
    #    from User Preferences.  The change set does not have to include any valid content.  This allows for
    #    the capture of inappropriate content being uploaded to new repositories.
    # 2. A regular user elects to receive email when the first valid change set is produced for a new repository
    #    from User Preferences.  This differs from 1 above in that the user will not receive email until a
    #    change set tha tincludes valid content is produced.
    # 3. An admin user checks the "Receive email alerts" check box on the manage repository page.  Since the
    #    user is an admin user, the email will include information about both HTML and image content that was
    #    included in the change set.
    # 4. A regular user checks the "Receive email alerts" check box on the manage repository page.  Since the
    #    user is not an admin user, the email will not include any information about both HTML and image content
    #    that was included in the change set.
    repo_dir = repository.repo_path( trans.app )
    repo = hg.repository( suc.get_configured_ui(), repo_dir )
    smtp_server = trans.app.config.smtp_server
    if smtp_server and ( new_repo_alert or repository.email_alerts ):
        # Send email alert to users that want them.
        if trans.app.config.email_from is not None:
            email_from = trans.app.config.email_from
        elif trans.request.host.split( ':' )[0] == 'localhost':
            email_from = 'galaxy-no-reply@' + socket.getfqdn()
        else:
            email_from = 'galaxy-no-reply@' + trans.request.host.split( ':' )[0]
        tip_changeset = repo.changelog.tip()
        ctx = repo.changectx( tip_changeset )
        t, tz = ctx.date()
        date = datetime( *gmtime( float( t ) - tz )[:6] )
        display_date = date.strftime( "%Y-%m-%d" )
        try:
            username = ctx.user().split()[0]
        except:
            username = ctx.user()
        # We'll use 2 template bodies because we only want to send content
        # alerts to tool shed admin users.
        if new_repo_alert:
            template = new_repo_email_alert_template
        else:
            template = email_alert_template
        admin_body = string.Template( template ).safe_substitute( host=trans.request.host,
                                                                  repository_name=repository.name,
                                                                  revision='%s:%s' %( str( ctx.rev() ), ctx ),
                                                                  display_date=display_date,
                                                                  description=ctx.description(),
                                                                  username=username,
                                                                  content_alert_str=content_alert_str )
        body = string.Template( template ).safe_substitute( host=trans.request.host,
                                                            repository_name=repository.name,
                                                            revision='%s:%s' %( str( ctx.rev() ), ctx ),
                                                            display_date=display_date,
                                                            description=ctx.description(),
                                                            username=username,
                                                            content_alert_str='' )
        admin_users = trans.app.config.get( "admin_users", "" ).split( "," )
        frm = email_from
        if new_repo_alert:
            subject = "Galaxy tool shed alert for new repository named %s" % str( repository.name )
            subject = subject[ :80 ]
            email_alerts = []
            for user in trans.sa_session.query( trans.model.User ) \
                                        .filter( and_( trans.model.User.table.c.deleted == False,
                                                       trans.model.User.table.c.new_repo_alert == True ) ):
                if admin_only:
                    if user.email in admin_users:
                        email_alerts.append( user.email )
                else:
                    email_alerts.append( user.email )
        else:
            subject = "Galaxy tool shed update alert for repository named %s" % str( repository.name )
            email_alerts = from_json_string( repository.email_alerts )
        for email in email_alerts:
            to = email.strip()
            # Send it
            try:
                if to in admin_users:
                    util.send_mail( frm, to, subject, admin_body, trans.app.config )
                else:
                    util.send_mail( frm, to, subject, body, trans.app.config )
            except Exception, e:
                log.exception( "An error occurred sending a tool shed repository update alert by email." )
def has_previous_repository_reviews( trans, repository, changeset_revision ):
    """Determine if a repository has a changeset revision review prior to the received changeset revision."""
    repo = hg.repository( suc.get_configured_ui(), repository.repo_path( trans.app ) )
    reviewed_revision_hashes = [ review.changeset_revision for review in repository.reviews ]
    for changeset in suc.reversed_upper_bounded_changelog( repo, changeset_revision ):
        previous_changeset_revision = str( repo.changectx( changeset ) )
        if previous_changeset_revision in reviewed_revision_hashes:
            return True
    return False
def new_repository_dependency_metadata_required( trans, repository, metadata_dict ):
    """
    Compare the last saved metadata for each repository dependency in the repository with the new 
    metadata in metadata_dict to determine if a new repository_metadata table record is required, 
    or if the last saved metadata record can be updated instead.
    """
    if 'repository_dependencies' in metadata_dict:
        repository_metadata = get_latest_repository_metadata( trans, repository.id )
        if repository_metadata:
            metadata = repository_metadata.metadata
            if metadata:
                if 'repository_dependencies' in metadata:
                    saved_repository_dependencies = metadata[ 'repository_dependencies' ][ 'repository_dependencies' ]
                    new_repository_dependencies = metadata_dict[ 'repository_dependencies' ][ 'repository_dependencies' ]
                    # The saved metadata must be a subset of the new metadata.
                    for new_repository_dependency_metadata in new_repository_dependencies:
                        if new_repository_dependency_metadata not in saved_repository_dependencies:
                            return True
                    for saved_repository_dependency_metadata in saved_repository_dependencies:
                        if saved_repository_dependency_metadata not in new_repository_dependencies:
                            return True
            else:
                # We have repository metadata that does not include metadata for any repository dependencies in the
                # repository, so we can update the existing repository metadata.
                return False
        else:
            # There is no saved repository metadata, so we need to create a new repository_metadata table record.
            return True
    # The received metadata_dict includes no metadata for repository dependencies, so a new repository_metadata table record is not needed.
    return False
def new_tool_metadata_required( trans, repository, metadata_dict ):
    """
    Compare the last saved metadata for each tool in the repository with the new metadata in metadata_dict to determine if a new repository_metadata
    table record is required, or if the last saved metadata record can be updated instead.
    """
    if 'tools' in metadata_dict:
        repository_metadata = get_latest_repository_metadata( trans, repository.id )
        if repository_metadata:
            metadata = repository_metadata.metadata
            if metadata:
                if 'tools' in metadata:
                    saved_tool_ids = []
                    # The metadata for one or more tools was successfully generated in the past
                    # for this repository, so we first compare the version string for each tool id
                    # in metadata_dict with what was previously saved to see if we need to create
                    # a new table record or if we can simply update the existing record.
                    for new_tool_metadata_dict in metadata_dict[ 'tools' ]:
                        for saved_tool_metadata_dict in metadata[ 'tools' ]:
                            if saved_tool_metadata_dict[ 'id' ] not in saved_tool_ids:
                                saved_tool_ids.append( saved_tool_metadata_dict[ 'id' ] )
                            if new_tool_metadata_dict[ 'id' ] == saved_tool_metadata_dict[ 'id' ]:
                                if new_tool_metadata_dict[ 'version' ] != saved_tool_metadata_dict[ 'version' ]:
                                    return True
                    # So far, a new metadata record is not required, but we still have to check to see if
                    # any new tool ids exist in metadata_dict that are not in the saved metadata.  We do
                    # this because if a new tarball was uploaded to a repository that included tools, it
                    # may have removed existing tool files if they were not included in the uploaded tarball.
                    for new_tool_metadata_dict in metadata_dict[ 'tools' ]:
                        if new_tool_metadata_dict[ 'id' ] not in saved_tool_ids:
                            return True
            else:
                # We have repository metadata that does not include metadata for any tools in the
                # repository, so we can update the existing repository metadata.
                return False
        else:
            # There is no saved repository metadata, so we need to create a new repository_metadata table record.
            return True
    # The received metadata_dict includes no metadata for tools, so a new repository_metadata table record is not needed.
    return False
def new_workflow_metadata_required( trans, repository, metadata_dict ):
    """
    Currently everything about an exported workflow except the name is hard-coded, so there's no real way to differentiate versions of
    exported workflows.  If this changes at some future time, this method should be enhanced accordingly.
    """
    if 'workflows' in metadata_dict:
        repository_metadata = get_latest_repository_metadata( trans, repository.id )
        if repository_metadata:
            # The repository has metadata, so update the workflows value - no new record is needed.
            return False
        else:
            # There is no saved repository metadata, so we need to create a new repository_metadata table record.
            return True
    # The received metadata_dict includes no metadata for workflows, so a new repository_metadata table record is not needed.
    return False
def set_repository_metadata( trans, repository, content_alert_str='', **kwd ):
    """
    Set metadata using the repository's current disk files, returning specific error messages (if any) to alert the repository owner that the changeset
    has problems.
    """
    message = ''
    status = 'done'
    encoded_id = trans.security.encode_id( repository.id )
    repository_clone_url = suc.generate_clone_url_for_repository_in_tool_shed( trans, repository )
    repo_dir = repository.repo_path( trans.app )
    repo = hg.repository( suc.get_configured_ui(), repo_dir )
    metadata_dict, invalid_file_tups = suc.generate_metadata_for_changeset_revision( app=trans.app,
                                                                                     repository=repository,
                                                                                     repository_clone_url=repository_clone_url,
                                                                                     relative_install_dir=repo_dir,
                                                                                     repository_files_dir=None,
                                                                                     resetting_all_metadata_on_repository=False,
                                                                                     updating_installed_repository=False,
                                                                                     persist=False )
    if metadata_dict:
        downloadable = suc.is_downloadable( metadata_dict )
        repository_metadata = None
        if new_repository_dependency_metadata_required( trans, repository, metadata_dict ) or \
           new_tool_metadata_required( trans, repository, metadata_dict ) or \
           new_workflow_metadata_required( trans, repository, metadata_dict ):
            # Create a new repository_metadata table row.
            repository_metadata = suc.create_or_update_repository_metadata( trans,
                                                                            encoded_id,
                                                                            repository,
                                                                            repository.tip( trans.app ),
                                                                            metadata_dict )
            # If this is the first record stored for this repository, see if we need to send any email alerts.
            if len( repository.downloadable_revisions ) == 1:
                handle_email_alerts( trans, repository, content_alert_str='', new_repo_alert=True, admin_only=False )
        else:
            repository_metadata = get_latest_repository_metadata( trans, repository.id )
            if repository_metadata:
                downloadable = suc.is_downloadable( metadata_dict )
                # Update the last saved repository_metadata table row.
                repository_metadata.changeset_revision = repository.tip( trans.app )
                repository_metadata.metadata = metadata_dict
                repository_metadata.downloadable = downloadable
                trans.sa_session.add( repository_metadata )
                trans.sa_session.flush()
            else:
                # There are no tools in the repository, and we're setting metadata on the repository tip.
                repository_metadata = suc.create_or_update_repository_metadata( trans,
                                                                                encoded_id,
                                                                                repository,
                                                                                repository.tip( trans.app ),
                                                                                metadata_dict )
        if 'tools' in metadata_dict and repository_metadata and status != 'error':
            # Set tool versions on the new downloadable change set.  The order of the list of changesets is critical, so we use the repo's changelog.
            changeset_revisions = []
            for changeset in repo.changelog:
                changeset_revision = str( repo.changectx( changeset ) )
                if suc.get_repository_metadata_by_changeset_revision( trans, encoded_id, changeset_revision ):
                    changeset_revisions.append( changeset_revision )
            add_tool_versions( trans, encoded_id, repository_metadata, changeset_revisions )
    elif len( repo ) == 1 and not invalid_file_tups:
        message = "Revision '%s' includes no tools, datatypes or exported workflows for which metadata can " % str( repository.tip( trans.app ) )
        message += "be defined so this revision cannot be automatically installed into a local Galaxy instance."
        status = "error"
    if invalid_file_tups:
        message = suc.generate_message_for_invalid_tools( trans, invalid_file_tups, repository, metadata_dict )
        status = 'error'
    # Reset the tool_data_tables by loading the empty tool_data_table_conf.xml file.
    suc.reset_tool_data_tables( trans.app )
    return message, status
def set_repository_metadata_due_to_new_tip( trans, repository, content_alert_str=None, **kwd ):
    # Set metadata on the repository tip.
    error_message, status = set_repository_metadata( trans, repository, content_alert_str=content_alert_str, **kwd )
    if error_message:
        # If there is an error, display it.
        return trans.response.send_redirect( web.url_for( controller='repository',
                                                          action='manage_repository',
                                                          id=trans.security.encode_id( repository.id ),
                                                          message=error_message,
                                                          status='error' ) )
def update_for_browsing( trans, repository, current_working_dir, commit_message='' ):
    # This method id deprecated, but we'll keep it around for a while in case we need it.  The problem is that hg purge
    # is not supported by the mercurial API.
    # Make a copy of a repository's files for browsing, remove from disk all files that are not tracked, and commit all
    # added, modified or removed files that have not yet been committed.
    repo_dir = repository.repo_path( trans.app )
    repo = hg.repository( suc.get_configured_ui(), repo_dir )
    # The following will delete the disk copy of only the files in the repository.
    #os.system( 'hg update -r null > /dev/null 2>&1' )
    files_to_remove_from_disk = []
    files_to_commit = []
    # We may have files on disk in the repo directory that aren't being tracked, so they must be removed.
    # The codes used to show the status of files are as follows.
    # M = modified
    # A = added
    # R = removed
    # C = clean
    # ! = deleted, but still tracked
    # ? = not tracked
    # I = ignored
    # We'll use mercurial's purge extension to remove untracked file.  Using this extension requires the
    # following entry in the repository's hgrc file which was not required for some time, so we'll add it
    # if it's missing.
    # [extensions]
    # hgext.purge=
    lines = repo.opener( 'hgrc', 'rb' ).readlines()
    if not '[extensions]\n' in lines:
        # No extensions have been added at all, so just append to the file.
        fp = repo.opener( 'hgrc', 'a' )
        fp.write( '[extensions]\n' )
        fp.write( 'hgext.purge=\n' )
        fp.close()
    elif not 'hgext.purge=\n' in lines:
        # The file includes and [extensions] section, but we need to add the
        # purge extension.
        fp = repo.opener( 'hgrc', 'wb' )
        for line in lines:
            if line.startswith( '[extensions]' ):
                fp.write( line )
                fp.write( 'hgext.purge=\n' )
            else:
                fp.write( line )
        fp.close()
    cmd = 'hg purge'
    os.chdir( repo_dir )
    proc = subprocess.Popen( args=cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT )
    return_code = proc.wait()
    os.chdir( current_working_dir )
    if return_code != 0:
        output = proc.stdout.read( 32768 )
        log.debug( 'hg purge failed in repository directory %s, reason: %s' % ( repo_dir, output ) )
    if files_to_commit:
        if not commit_message:
            commit_message = 'Committed changes to: %s' % ', '.join( files_to_commit )
        repo.dirstate.write()
        repo.commit( user=trans.user.username, text=commit_message )
    cmd = 'hg update > /dev/null 2>&1'
    os.chdir( repo_dir )
    proc = subprocess.Popen( args=cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT )
    return_code = proc.wait()
    os.chdir( current_working_dir )
    if return_code != 0:
        output = proc.stdout.read( 32768 )
        log.debug( 'hg update > /dev/null 2>&1 failed in repository directory %s, reason: %s' % ( repo_dir, output ) )