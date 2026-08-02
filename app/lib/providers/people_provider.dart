import 'package:just_audio/just_audio.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/providers/base_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';

typedef CreatePersonRequest = Future<Person?> Function(String name, String expectedAuthenticatedUid);
typedef UpdatePersonRequest = Future<bool> Function(String personId, String name, String expectedAuthenticatedUid);
typedef DeletePersonRequest = Future<bool> Function(String personId, String expectedAuthenticatedUid);
typedef DeletePersonSampleRequest = Future<bool> Function(
  String personId,
  int sampleIndex,
  String expectedAuthenticatedUid,
);

class PeopleProvider extends BaseProvider {
  PeopleProvider({
    Future<List<Person>> Function()? fetchPeople,
    SharedPreferencesUtil? preferences,
    ActiveAccountAuthorityProvider? activeAuthority,
    CreatePersonRequest? createPersonRequest,
    UpdatePersonRequest? updatePersonRequest,
    DeletePersonRequest? deletePersonRequest,
    DeletePersonSampleRequest? deleteSampleRequest,
  })  : _fetchPeople = fetchPeople ?? getAllPeople,
        _preferences = preferences ?? SharedPreferencesUtil(),
        _activeAuthority = activeAuthority ?? WalOwnerAuthority.activeAccount,
        _createPersonRequest =
            createPersonRequest ?? ((name, expectedUid) => createPerson(name, expectedAuthenticatedUid: expectedUid)),
        _updatePersonRequest = updatePersonRequest ??
            ((personId, name, expectedUid) => updatePersonName(personId, name, expectedAuthenticatedUid: expectedUid)),
        _deletePersonRequest = deletePersonRequest ??
            ((personId, expectedUid) => deletePerson(personId, expectedAuthenticatedUid: expectedUid)),
        _deleteSampleRequest = deleteSampleRequest ??
            ((personId, sampleIndex, expectedUid) => deletePersonSpeechSample(
                  personId,
                  sampleIndex,
                  expectedAuthenticatedUid: expectedUid,
                )) {
    people = _preferences.cachedPeople;
  }

  final Future<List<Person>> Function() _fetchPeople;
  final SharedPreferencesUtil _preferences;
  final ActiveAccountAuthorityProvider _activeAuthority;
  final CreatePersonRequest _createPersonRequest;
  final UpdatePersonRequest _updatePersonRequest;
  final DeletePersonRequest _deletePersonRequest;
  final DeletePersonSampleRequest _deleteSampleRequest;
  int _operationGeneration = 0;
  late List<Person> people;
  Map<String, List<String>> samplesUrl = {};

  final AudioPlayer _audioPlayer = AudioPlayer();
  int? currentPlayingPersonIndex;
  int? currentPlayingIndex;
  bool isPlaying = false;

  void initialize() {
    loading = true;
    notifyListeners();
    setPeople();
    _setupAudioPlayerListeners();
  }

  void reset() {
    _operationGeneration++;
    people = [];
    samplesUrl = {};
    currentPlayingPersonIndex = null;
    currentPlayingIndex = null;
    isPlaying = false;
    notifyListeners();
  }

  EllaAccountCommitLease? _beginAccountCommit() =>
      EllaAccountCommitBarrier.begin(authorityProvider: _activeAuthority, onInvalidated: reset);

  bool _canCommit(EllaAccountCommitLease lease, int generation) =>
      generation == _operationGeneration && lease.isCurrent;

  setPeople() async {
    final lease = _beginAccountCommit();
    if (lease == null) {
      loading = false;
      return;
    }
    final generation = _operationGeneration;
    try {
      final value = await _fetchPeople();
      if (generation != _operationGeneration || !lease.isCurrent) return;
      loading = false;
      people = value;
      _preferences.cachedPeople = people;
      Logger.debug('${people.length} people refreshed');
      notifyListeners();
    } finally {
      lease.close();
    }
  }

  void _setupAudioPlayerListeners() {
    _audioPlayer.playerStateStream.listen((playerState) {
      if (playerState.processingState == ProcessingState.completed) {
        currentPlayingPersonIndex = null;
        currentPlayingIndex = null;
        isPlaying = false;
      }
    });
  }

  Future<void> playPause(int personIdx, int sampleIdx, String fileUrl) async {
    if (currentPlayingPersonIndex == personIdx && currentPlayingIndex == sampleIdx) {
      if (isPlaying) {
        _audioPlayer.pause();
        isPlaying = false;
      } else {
        _audioPlayer.play();
        isPlaying = true;
      }
      notifyListeners();
    } else {
      _audioPlayer.stop();
      await _audioPlayer.setUrl(fileUrl);
      currentPlayingPersonIndex = personIdx;
      currentPlayingIndex = sampleIdx;
      isPlaying = true; // setState?
      notifyListeners();
      await _audioPlayer.play();
    }
  }

  Future<Person?> createPersonProvider(String name) async {
    if (loading) return null;
    final lease = _beginAccountCommit();
    if (lease == null) return null;
    final generation = _operationGeneration;
    loading = true;
    notifyListeners();

    try {
      final newPerson = await _createPersonRequest(name, lease.authority.uid);
      if (!_canCommit(lease, generation) || newPerson == null) return null;

      people.add(newPerson);
      people.sort((a, b) => a.name.compareTo(b.name));
      _preferences.cachedPeople = people;

      return newPerson;
    } finally {
      if (_canCommit(lease, generation)) {
        loading = false;
        notifyListeners();
      }
      lease.close();
    }
  }

  Future<void> updatePersonProvider(Person person, String name) async {
    if (loading) return;
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    loading = true;
    notifyListeners();

    try {
      final success = await _updatePersonRequest(person.id, name, lease.authority.uid);
      if (!success || !_canCommit(lease, generation)) return;
      final index = people.indexWhere((p) => p.id == person.id);
      if (index != -1) {
        people[index] = Person(
          id: person.id,
          name: name,
          createdAt: person.createdAt,
          updatedAt: DateTime.now(),
          speechSamples: person.speechSamples,
        );
        people.sort((a, b) => a.name.compareTo(b.name));
        _preferences.cachedPeople = people;
      }
    } finally {
      if (_canCommit(lease, generation)) {
        loading = false;
        notifyListeners();
      }
      lease.close();
    }
  }

  Future<void> deletePersonSample(int personIdx, int sampleIdx) async {
    if (personIdx < 0 || personIdx >= people.length) return;
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    final personId = people[personIdx].id;

    try {
      final success = await _deleteSampleRequest(personId, sampleIdx, lease.authority.uid);
      if (!_canCommit(lease, generation)) return;
      final currentIndex = people.indexWhere((person) => person.id == personId);
      if (success && currentIndex != -1) {
        final samples = people[currentIndex].speechSamples;
        if (samples != null && sampleIdx >= 0 && sampleIdx < samples.length) {
          samples.removeAt(sampleIdx);
          _preferences.replaceCachedPerson(people[currentIndex]);
          notifyListeners();
        }
      } else if (!success) {
        Logger.debug('Failed to delete speech sample at index: $sampleIdx');
      }
    } finally {
      lease.close();
    }
  }

  Future<void> deletePersonProvider(Person person) async {
    final lease = _beginAccountCommit();
    if (lease == null) return;
    final generation = _operationGeneration;
    try {
      final success = await _deletePersonRequest(person.id, lease.authority.uid);
      if (!success || !_canCommit(lease, generation)) return;
      people.removeWhere((candidate) => candidate.id == person.id);
      _preferences.cachedPeople = people;
      notifyListeners();
    } finally {
      lease.close();
    }
  }

  @override
  void dispose() {
    _audioPlayer.dispose();
    super.dispose();
  }
}
