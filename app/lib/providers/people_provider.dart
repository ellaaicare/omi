import 'package:just_audio/just_audio.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/providers/base_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/logger.dart';

typedef CreatePersonRequest = Future<Person?> Function(String name, ExactAccountAuthorityVerifier exactAuthority);
typedef FetchPeopleRequest = Future<List<Person>> Function(ExactAccountAuthorityVerifier exactAuthority);
typedef UpdatePersonRequest = Future<bool> Function(
  String personId,
  String name,
  ExactAccountAuthorityVerifier exactAuthority,
);
typedef DeletePersonRequest = Future<bool> Function(String personId, ExactAccountAuthorityVerifier exactAuthority);
typedef DeletePersonSampleRequest = Future<bool> Function(
  String personId,
  int sampleIndex,
  ExactAccountAuthorityVerifier exactAuthority,
);

class PeopleProvider extends BaseProvider {
  PeopleProvider({
    FetchPeopleRequest? fetchPeople,
    SharedPreferencesUtil? preferences,
    ActiveAccountAuthorityProvider? activeAuthority,
    CreatePersonRequest? createPersonRequest,
    UpdatePersonRequest? updatePersonRequest,
    DeletePersonRequest? deletePersonRequest,
    DeletePersonSampleRequest? deleteSampleRequest,
  })  : _fetchPeople = fetchPeople ?? ((authority) => getAllPeople(exactAuthority: authority)),
        _preferences = preferences ?? SharedPreferencesUtil(),
        _activeAuthority = activeAuthority ?? WalOwnerAuthority.activeAccount,
        _createPersonRequest =
            createPersonRequest ?? ((name, authority) => createPerson(name, exactAuthority: authority)),
        _updatePersonRequest = updatePersonRequest ??
            ((personId, name, authority) => updatePersonName(personId, name, exactAuthority: authority)),
        _deletePersonRequest =
            deletePersonRequest ?? ((personId, authority) => deletePerson(personId, exactAuthority: authority)),
        _deleteSampleRequest = deleteSampleRequest ??
            ((personId, sampleIndex, authority) => deletePersonSpeechSample(
                  personId,
                  sampleIndex,
                  exactAuthority: authority,
                )) {
    people = _preferences.cachedPeople;
  }

  final FetchPeopleRequest _fetchPeople;
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
    loading = false;
    notifyListeners();
  }

  EllaAccountCommitLease? _beginAccountCommit() =>
      EllaAccountCommitBarrier.begin(authorityProvider: _activeAuthority, onInvalidated: reset);

  bool _canCommit(EllaAccountCommitLease lease, int generation) =>
      generation == _operationGeneration && lease.isCurrent;

  Future<void> setPeople() async {
    final generation = ++_operationGeneration;
    loading = true;
    notifyListeners();
    final lease = _beginAccountCommit();
    if (lease == null) {
      _finishPeopleLoad(generation);
      return;
    }
    try {
      final value = await _fetchPeople(lease);
      if (generation != _operationGeneration || !lease.isCurrent) return;
      people = value;
      _preferences.cachedPeople = people;
      Logger.debug('${people.length} people refreshed');
    } on ExactAccountAuthorityChangedException {
      Logger.debug('People refresh discarded after account authority changed');
    } finally {
      _finishPeopleLoad(generation);
      lease.close();
    }
  }

  void _finishPeopleLoad(int generation) {
    if (generation != _operationGeneration) return;
    loading = false;
    notifyListeners();
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
      final newPerson = await _createPersonRequest(name, lease);
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
      final success = await _updatePersonRequest(person.id, name, lease);
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
      final success = await _deleteSampleRequest(personId, sampleIdx, lease);
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
      final success = await _deletePersonRequest(person.id, lease);
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
