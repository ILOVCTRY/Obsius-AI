
// Provide a default path to dwr.engine
if (dwr == null) var dwr = {};
if (dwr.engine == null) dwr.engine = {};
if (DWREngine == null) var DWREngine = dwr.engine;

if (NewsvoteDWR == null) var NewsvoteDWR = {};
NewsvoteDWR._path = '/system/dwr';
NewsvoteDWR.isVote = function(p0, p1, p2, callback) {
  dwr.engine._execute(NewsvoteDWR._path, 'NewsvoteDWR', 'isVote', p0, p1, p2, callback);
}
NewsvoteDWR.addLinkClickTimes = function(p0, p1, p2, callback) {
  dwr.engine._execute(NewsvoteDWR._path, 'NewsvoteDWR', 'addLinkClickTimes', p0, p1, p2, callback);
}
NewsvoteDWR.getLinkClickTimes = function(p0, p1, p2, p3, callback) {
  dwr.engine._execute(NewsvoteDWR._path, 'NewsvoteDWR', 'getLinkClickTimes', p0, p1, p2, p3, callback);
}
NewsvoteDWR.getNewsLinkUrl = function(p0, p1, p2, callback) {
  dwr.engine._execute(NewsvoteDWR._path, 'NewsvoteDWR', 'getNewsLinkUrl', p0, p1, p2, callback);
}
NewsvoteDWR.getVoteResult = function(p0, p1, callback) {
  dwr.engine._execute(NewsvoteDWR._path, 'NewsvoteDWR', 'getVoteResult', p0, p1, false, callback);
}
NewsvoteDWR.getVoteTitle = function(p0, p1, callback) {
  dwr.engine._execute(NewsvoteDWR._path, 'NewsvoteDWR', 'getVoteTitle', p0, p1, callback);
}
NewsvoteDWR.save = function(p0, p1, p2, p3, callback) {
  dwr.engine._execute(NewsvoteDWR._path, 'NewsvoteDWR', 'save', p0, p1, p2, p3, callback);
}
NewsvoteDWR.getResult = function(p0, p1, callback) {
  dwr.engine._execute(NewsvoteDWR._path, 'NewsvoteDWR', 'getResult', p0, p1, callback);
}
