@Library('jenkins-shared-library@main') _

standardBuild(
  version: 2,
  skipBranchBuilds: true,
  jobDescription: 'Uploads flask-peewee module to private devpi repository',
  properties: [buildDiscarder(logRotator(artifactDaysToKeepStr: '', artifactNumToKeepStr: '', daysToKeepStr: '', numToKeepStr: '10'))],
) { TAG, BRANCH_NAME, GIT_SHA ->

  // Upload flask-peewee to private devpi repository
  stage("Upload flask-peewee") {
    if (BRANCH_NAME == config.mainBranch) {
      utils.uploadPyModule(path: ".")
    }
  }
}
